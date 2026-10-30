"""Проверка цифр кодом, без нейросети.

Каждое число из поста (возраст, сумма, дата, количество, проценты) ищем в исходных статьях.
Нейросеть может перепутать «6.000 tỷ» и «6 млрд» или придумать возраст — программа это ловит.
Число считается подтверждённым, если оно есть хотя бы в одной статье — точно или как округление
(«больше 5,6 млрд» при «5,65 tỷ» в статье)."""
import math
import re

# --- Русский пост ---
RU_UNITS = {
    "тыс": 1e3, "тысяч": 1e3, "тысячи": 1e3, "тысяча": 1e3, "тысячу": 1e3,
    "млн": 1e6, "миллион": 1e6, "миллиона": 1e6, "миллионов": 1e6,
    "млрд": 1e9, "миллиард": 1e9, "миллиарда": 1e9, "миллиардов": 1e9,
    "трлн": 1e12, "триллион": 1e12, "триллиона": 1e12, "триллионов": 1e12,
}
_RU_UNIT_RX = "|".join(sorted((re.escape(u) for u in RU_UNITS), key=len, reverse=True))
RU_NUM = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[   ]\d{3})+|\d+)(?:([.,])(\d+))?"
    r"(?:\s*(" + _RU_UNIT_RX + r")(?![а-яё]))?",
    re.IGNORECASE,
)

# --- Вьетнамские и английские статьи ---
SRC_UNITS = [
    (r"nghìn\s+tỷ|ngàn\s+tỷ|nghìn\s+tỉ|ngàn\s+tỉ", 1e12),
    (r"tỷ|tỉ|billion|bn", 1e9),
    (r"triệu|million|mln", 1e6),
    (r"nghìn|ngàn|thousand|k", 1e3),
    (r"trillion", 1e12),
]
SRC_NUM = re.compile(r"(?<!\d)(\d[\d.,]*\d|\d)(?:\s*(" + "|".join(u for u, _ in SRC_UNITS) + r")(?!\w))?",
                     re.IGNORECASE)
WORDS = {
    "một": 1, "hai": 2, "ba": 3, "bốn": 4, "tư": 4, "năm": 5, "sáu": 6, "bảy": 7, "bẩy": 7, "tám": 8,
    "chín": 9, "mười": 10, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "đôi": 2,
}


def _unit_size(word: str) -> float:
    for rx, size in SRC_UNITS:
        if re.fullmatch(rx, word, re.IGNORECASE):
            return size
    return 1.0


def source_numbers(text: str) -> set:
    """Все числа статьи во всех разумных прочтениях: «6.000 tỷ» → 6000, 6e12; «39,6» → 39.6 и 396."""
    vals = set()
    for m in SRC_NUM.finditer(text):
        raw, unit = m.group(1), m.group(2)
        size = _unit_size(unit) if unit else 1.0
        cands = set()
        for part in re.split(r"[.,]", raw):            # 15.10.2026 → 15, 10, 2026
            if part:
                cands.add(float(part))
        cands.add(float(re.sub(r"[.,]", "", raw)))     # тысячи через точку или запятую
        if re.fullmatch(r"\d+(?:\.\d{3})*,\d+", raw):  # вьетнамская дробь: 1.234,5
            cands.add(float(raw.replace(".", "").replace(",", ".")))
        if re.fullmatch(r"\d+(?:,\d{3})*\.\d+", raw):  # английская дробь: 1,234.5
            cands.add(float(raw.replace(",", "")))
        if re.fullmatch(r"\d+[.,]\d{1,2}", raw):       # «5,6 tỷ», «2.5 million»
            cands.add(float(raw.replace(",", ".")))
        for c in cands:
            vals.add(c)
            vals.add(c * size)
    low = text.lower()
    for word, n in WORDS.items():
        if re.search(rf"(?<!\w){word}(?!\w)", low):
            vals.add(float(n))
    return vals


def post_numbers(text: str) -> list:
    """[(как написано, значение, единица, знаков после запятой, части)] для каждого числа в посте."""
    out = []
    for m in RU_NUM.finditer(text):
        whole = re.sub(r"[   ]", "", m.group(1))
        sep, frac, unit = m.group(2), m.group(3) or "", (m.group(4) or "").lower()
        size = RU_UNITS.get(unit, 1.0)
        parts = [float(whole)] + ([float(frac)] if frac else [])
        mantissa = float(f"{whole}.{frac}") if frac else float(whole)
        out.append((m.group(0).strip(), mantissa * size, size, len(frac), parts, sep == "."))
    return out


def _matches(value: float, size: float, decimals: int, sources: set) -> bool:
    for s in sources:
        if math.isclose(value, s, rel_tol=1e-9, abs_tol=1e-9):
            return True
        if s >= size and s > 0:
            q = s / size
            target = value / size
            scale = 10 ** decimals
            if math.isclose(round(q * scale) / scale, target, abs_tol=1e-9) or \
               math.isclose(math.floor(q * scale) / scale, target, abs_tol=1e-9):
                return True
    return False


def check_numbers(post_text: str, articles_text: str) -> list:
    """Список проблем: числа из поста, которых нет ни в одной статье."""
    sources = source_numbers(articles_text)
    problems, seen = [], set()
    for raw, value, size, decimals, parts, dotted in post_numbers(post_text):
        if raw in seen:
            continue
        seen.add(raw)
        if _matches(value, size, decimals, sources):
            continue
        # «15.10» в посте может быть датой — тогда обе части должны быть в статье
        if dotted and all(_matches(p, 1.0, 0, sources) for p in parts):
            continue
        problems.append(f"цифры «{raw}» нет в статьях")
    return problems
