"""Пересчёт сумм в донгах."""
from bot import money
from bot.store import Store

R = {"usd_rub": 83.4839, "vnd_rub": 32.5651 / 10000}


def test_big_sums_become_readable_and_converted():
    assert money.annotate("прошло 6000 миллиардов донгов", R) == "прошло 6 трлн донгов (≈ $234 млн ≈ 19,5 млрд ₽)"
    assert money.annotate("оборот 6 000 млрд донгов", R).startswith("оборот 6 трлн донгов")
    assert money.annotate("на 39,6 млрд донгов", R) == "на 39,6 млрд донгов (≈ $1,54 млн ≈ 129 млн ₽)"
    assert money.annotate("штраф 75 млн донгов", R) == "штраф 75 млн донгов (≈ $2 930 ≈ 244 000 ₽)"
    assert money.annotate("и 100 000 донгов", R) == "и 100 000 донгов (≈ $3,9 ≈ 326 ₽)"


def test_without_rates_only_normalizes():
    assert money.annotate("6000 млрд донгов", {}) == "6 трлн донгов"


def test_untouched_text():
    assert money.annotate("5 донгов и 6.000 tỷ đồng", R) == "5 донгов и 6.000 tỷ đồng"
    assert money.annotate("500 тыс. юаней", R) == "500 тыс. юаней"


def test_rates_cached_per_day(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    calls = []

    class Resp:
        def json(self):
            calls.append(1)
            return {"Date": "2026-10-16T11:30:00+03:00", "Valute": {
                "USD": {"Nominal": 1, "Value": 83.48}, "VND": {"Nominal": 10000, "Value": 32.56}}}

    monkeypatch.setattr(money.httpx, "get", lambda *a, **k: Resp())
    r1 = money.fetch_rates(store)
    r2 = money.fetch_rates(store)
    assert len(calls) == 1 and abs(r2["vnd_rub"] - 0.003256) < 1e-9

    def boom(*a, **k):
        raise OSError("нет сети")

    monkeypatch.setattr(money.httpx, "get", boom)
    store.kv_set("rates", '{"usd_rub": 80, "vnd_rub": 0.003, "fetched": "2000-01-01"}')
    assert money.fetch_rates(store)["usd_rub"] == 80          # сайт недоступен — последний сохранённый курс
