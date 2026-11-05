"""Суммы в донгах: приводим к понятному виду и добавляем пересчёт в доллары и рубли.

Пересчёт делает код, а не нейросеть, — так цифры не перепутаются.
Курс берём у ЦБ РФ (зеркало cbr-xml-daily.ru), раз в день; если сайт недоступен — последний сохранённый."""
import json
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"

UNITS = {
    "тыс": 1e3, "тысяч": 1e3, "тысячи": 1e3, "тысяча": 1e3,
    "млн": 1e6, "миллион": 1e6, "миллиона": 1e6, "миллионов": 1e6,
    "млрд": 1e9, "миллиард": 1e9, "миллиарда": 1e9, "миллиардов": 1e9,
    "трлн": 1e12, "триллион": 1e12, "триллиона": 1e12, "триллионов": 1e12,
}

_UNIT_RX = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
# «6 000 млрд донгов», «39,6 млрд донгов», «100 000 донгов», «1,2 трлн вьетнамских донгов»
DONG_RX = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:[.,](\d+))?"
    r"\s*(?:(" + _UNIT_RX + r")\.?\s*)?(?:вьетнамских\s+)?(донг(?:ов|а|и)?|VND|VNĐ|đồng)(?![а-яё\w])",
    re.IGNORECASE,
)


def fetch_rates(store=None) -> dict:
    """{'usd_rub': рублей за доллар, 'vnd_rub': рублей за 1 донг, 'date': '…'} или {}."""
    today = time.strftime("%Y-%m-%d")
    cached = {}
    if store is not None:
        try:
            cached = json.loads(store.kv_get("rates", "") or "{}")
        except ValueError:
            cached = {}
        if cached.get("fetched") == today:
            return cached
    try:
        data = httpx.get(CBR_URL, timeout=15).json()
        usd = data["Valute"]["USD"]["Value"] / data["Valute"]["USD"]["Nominal"]
        vnd = data["Valute"]["VND"]["Value"] / data["Valute"]["VND"]["Nominal"]
        rates = {"usd_rub": usd, "vnd_rub": vnd, "date": data.get("Date", "")[:10], "fetched": today}
        if store is not None:
            store.kv_set("rates", json.dumps(rates))
        return rates
    except Exception as err:  # noqa: BLE001
        log.warning("Курс ЦБ не получен (%s), беру последний сохранённый", err)
        return cached


def _num(x: float) -> str:
    """1234.5 → «1 234,5», без лишних нулей."""
    if x >= 100:
        s = f"{round(x):,}".replace(",", " ")
    elif x >= 10:
        s = f"{x:.1f}".rstrip("0").rstrip(".")
    else:
        s = f"{x:.2f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def human(value: float, currency: str) -> str:
    """6e12, 'донгов' → «6 трлн донгов»; 2.3e8, '$' → «$230 млн». Пересчёт округляем до 3 значащих цифр."""
    if currency != "донгов" and value >= 1000:
        from math import floor, log10
        step = 10 ** (floor(log10(value)) - 2)
        value = round(value / step) * step
    for unit, size in (("трлн", 1e12), ("млрд", 1e9), ("млн", 1e6)):
        if value >= size:
            text = f"{_num(value / size)} {unit}"
            break
    else:
        text = _num(value)
    if currency == "$":
        return f"${text}"
    return f"{text} {currency}"


def annotate(text: str, rates: dict) -> str:
    """Каждую сумму в донгах переписываем понятно и добавляем «≈ $… ≈ … ₽»."""
    if not text:
        return text

    def repl(m: re.Match) -> str:
        whole = re.sub(r"[   ]", "", m.group(1))
        frac = m.group(2) or ""
        unit = (m.group(3) or "").lower().rstrip(".")
        value = float(f"{whole}.{frac}" if frac else whole) * UNITS.get(unit, 1)
        if value < 1000:  # «5 донгов» — не сумма, а, скорее всего, ошибка разбора
            return m.group(0)
        out = human(value, "донгов")
        if rates.get("vnd_rub") and rates.get("usd_rub"):
            rub = value * rates["vnd_rub"]
            usd = rub / rates["usd_rub"]
            out += f" (≈ {human(usd, '$')} ≈ {human(rub, '₽')})"
        return out

    return DONG_RX.sub(repl, text)


# «312 000 USD», «312 000 долларов», «$312 000», «1,2 млн USD»
_NUM = r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,](\d+))?"
USD_RX = re.compile(
    r"(?:\$\s*" + _NUM + r"(?:\s*(" + _UNIT_RX + r")\.?(?![а-яё\w]))?"
    r"|(?<![\d.,$])" + _NUM + r"\s*(?:(" + _UNIT_RX + r")\.?\s*)?(?:USD|долл(?:ар(?:ов|а|ы)?)?\.?\s*(?:США)?)(?![а-яё\w]))",
    re.IGNORECASE,
)


def annotate_usd(text: str, rates: dict) -> str:
    """Суммы в долларах: единый вид «$312 000» и пересчёт в рубли, если он ещё не стоит рядом."""
    if not text:
        return text

    def repl(m: re.Match) -> str:
        whole, frac, unit = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(4), m.group(5), m.group(6))
        whole = re.sub(r"[ \u00a0\u202f]", "", whole)
        value = float(f"{whole}.{frac}" if frac else whole) * UNITS.get((unit or "").lower().rstrip("."), 1)
        if value < 1:
            return m.group(0)
        out = "$" + (human(value, "$")[1:] if value >= 1e6 else _num(value) if value < 100 else f"{round(value):,}".replace(",", " "))
        after = text[m.end():m.end() + 6]
        if rates.get("usd_rub") and "₽" not in after and "≈" not in after:
            out += f" (≈ {human(value * rates['usd_rub'], '₽')})"
        return out

    # пересчёт донгов уже даёт «(≈ $… ≈ … ₽)» — его не трогаем
    parts = re.split(r"(\(≈[^)]*\))", text)
    return "".join(x if x.startswith("(≈") else USD_RX.sub(repl, x) for x in parts)


def annotate_all(text: str, rates: dict) -> str:
    return annotate_usd(annotate(text, rates), rates)


def annotate_brief(data: dict, rates: dict) -> dict:
    data = dict(data)
    for key in ("headline", "summary", "why"):
        data[key] = annotate(data.get(key, ""), rates)
    for key in ("facts", "numbers"):
        data[key] = [annotate(x, rates) for x in data.get(key, [])]
    return data
