"""Проверка конвейера без сети: сайты, нейросеть и Telegram подменены заглушками.
Запуск: python -m pytest tests -q"""
import time

import pytest


from bot import fetch, llm, main  # noqa: E402
from bot.config import CFG  # noqa: E402
from bot.fetch import Article, Entry, parse_listing, parse_rss  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.telegram import Telegram, build_post  # noqa: E402

@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(CFG, "dry_run", True)
    monkeypatch.setattr(CFG, "mode", "auto")
    monkeypatch.setattr(CFG, "min_gap_min", 0)
    monkeypatch.setattr(CFG, "quiet_from", 0)
    monkeypatch.setattr(CFG, "quiet_to", 0)


THANHNIEN_RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Pháp luật</title>
<item><title><![CDATA[Phá đường dây giả mạo khách sạn, resort ở Phú Quốc để lừa đảo: Dòng tiền hơn 6.000 tỉ đồng]]></title>
<link>https://thanhnien.vn/pha-duong-day-185261013192743618.htm</link>
<description><![CDATA[<p>Công an An Giang...</p>]]></description>
<pubDate>Tue, 13 Oct 26 20:12:00 +0700</pubDate></item>
<item><title>Phát hiện 451 cây rừng bị đốn hạ trái phép ở Gia Lai</title>
<link>https://thanhnien.vn/cay-rung.htm</link><pubDate>Thu, 15 Oct 26 13:17:00 +0700</pubDate></item>
</channel></rss>"""

BKH_HTML = """<html><body>
<a href="/phap-luat/an-ninh-trat-tu/202610/luc-luong-cuu-ho-bai-bien-kip-thoi-khong-che-doi-tuong-giat-tui-xach-du-khach-1a1385e/">
 Lực lượng cứu hộ bãi biển kịp thời khống chế đối tượng giật túi xách du khách</a>
<a href="/phap-luat/202610/bo-tu-phap-to-chuc-boi-duong-chuyen-sau-ve-phap-luat-dan-su-67f7ab8/">Bộ Tư pháp tổ chức bồi dưỡng chuyên sâu về pháp luật dân sự</a>
<a href="/video/">Video</a></body></html>"""


def test_parse_rss_two_digit_year():
    items = parse_rss(THANHNIEN_RSS, "Thanh Niên")
    assert len(items) == 2
    assert "Phú Quốc" in items[0].title
    assert time.gmtime(items[0].published_at).tm_year == 2026


def test_parse_listing():
    items = parse_listing(BKH_HTML, "https://baokhanhhoa.vn/phap-luat/",
                          r"^https://baokhanhhoa\.vn/[a-z0-9\-/]+/20\d{4}/[a-z0-9\-]+/?$", "Báo Khánh Hòa")
    assert len(items) == 2 and all(i.url.startswith("https://baokhanhhoa.vn/") for i in items)


def test_build_post_escapes_html():
    post = build_post({"emoji": "🚨", "headline": "Туристов <ограбили>", "body": "A & B",
                       "takeaway": "", "hashtags": ["Нячанг"]}, "https://x.vn/a?b=1&c=2", "Báo Khánh Hòa")
    assert "&lt;ограбили&gt;" in post and "A &amp; B" in post and "#Нячанг" in post and "👉" not in post


def test_full_pipeline(tmp_path, monkeypatch):
    now = time.time()
    entries = [
        Entry("https://bkh.vn/giat-tui", "Lực lượng cứu hộ bãi biển kịp thời khống chế đối tượng giật túi xách du khách", "", now, "Báo Khánh Hòa"),
        Entry("https://bkh.vn/hoi-nghi", "Bộ Tư pháp tổ chức bồi dưỡng chuyên sâu về pháp luật dân sự", "", now, "Báo Khánh Hòa"),
        Entry("https://vne.vn/phu-quoc", "Giả resort 5 sao ở Phú Quốc để lừa đặt cọc, dòng tiền hơn 6.000 tỷ đồng", "", now, "VnExpress"),
        Entry("https://tn.vn/phu-quoc", "Phá đường dây giả mạo khách sạn, resort ở Phú Quốc để lừa đảo", "", now, "Thanh Niên"),
        Entry("https://tn.vn/old", "Cướp giật điện thoại du khách", "", now - 5 * 86400, "Thanh Niên"),
        Entry("https://vne.vn/ga", "Ngã ba Trần Phú ùn tắc giờ cao điểm", "", now, "VnExpress"),
    ]
    monkeypatch.setattr(main, "SOURCES", ["one-source"])
    monkeypatch.setattr(main, "fetch_source", lambda src: entries)
    monkeypatch.setattr(main, "fetch_article", lambda url: Article("Nội dung bài báo " * 40, "https://img/x.jpg", None))

    scored = []

    def fake_score(title, summary, text, source, recent):
        scored.append(title)
        if "Phá đường dây" in title:  # та же история с другого сайта
            return {"score": 9, "category": "scam", "title_ru": "Фейковые отели Фукуока", "duplicate_of": recent[0]["id"], "reason": "dup"}
        if "Phú Quốc" in title:
            return {"score": 9, "category": "scam", "title_ru": "Фейковые отели Фукуока", "duplicate_of": 0, "reason": "ok"}
        return {"score": 10, "category": "russians", "title_ru": "У русских туристов вырвали сумку", "duplicate_of": 0, "reason": "ok"}

    monkeypatch.setattr(llm, "score", fake_score)
    monkeypatch.setattr(llm, "write_post", lambda *a, **k: {
        "emoji": "🚨", "headline": "Заголовок", "body": "Текст", "takeaway": "Держите сумку", "hashtags": ["#Нячанг"]})
    checks = iter([{"ok": False, "problems": ["лишняя цифра"]}, {"ok": True, "problems": []},
                   {"ok": True, "problems": []}])
    monkeypatch.setattr(llm, "fact_check", lambda *a: next(checks))

    store = Store(str(tmp_path / "t.db"))
    tg = Telegram("x")
    main.collect(store)
    main.process(store, tg)

    statuses = {r["url"]: r["status"] for r in store.db.execute("SELECT url, status FROM items")}
    assert "https://tn.vn/old" not in statuses                       # старьё не берём
    assert statuses["https://bkh.vn/hoi-nghi"] == "filtered"          # скучное отсечено без нейросети
    assert statuses["https://vne.vn/ga"] == "filtered"                # «Ngã ba» ≠ «Nga»
    assert statuses["https://bkh.vn/giat-tui"] == "queued"
    assert statuses["https://vne.vn/phu-quoc"] == "queued"
    assert statuses["https://tn.vn/phu-quoc"] == "duplicate"          # дубль с другого сайта
    assert len(scored) == 3

    main.publish_from_queue(store, tg)
    posted = store.by_status("posted")
    assert len(posted) == 1 and posted[0]["score"] == 10              # первым уходит самое интересное


def test_daily_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "max_posts_per_day", 1)
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "s", "t", "", time.time())
    store.update(1, status="posted", posted_at=time.time())
    assert main.can_post_now(store) is False


def test_late_button_press_still_works(tmp_path, monkeypatch):
    """При запуске по расписанию Telegram уже не принимает ответ на старую кнопку — действие всё равно выполняется."""
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "s", "t", "", time.time())
    store.update(1, status="review", post_html="<b>пост</b>")
    tg = Telegram("x")
    upd = [{"update_id": 7, "callback_query": {"id": "c", "data": "q:1", "from": {"id": 42},
                                               "message": {"chat": {"id": 42}, "message_id": 5}}}]
    monkeypatch.setattr(tg, "get_updates", lambda offset, timeout: upd)

    def too_old(*a, **k):
        raise RuntimeError("query is too old")

    monkeypatch.setattr(tg, "answer_callback", too_old)
    monkeypatch.setattr(tg, "edit_buttons", too_old)
    main.handle_updates(store, tg, timeout=0)
    assert store.get(1)["status"] == "queued"
    assert store.kv_get("tg_offset") == "8"


def test_prune_keeps_db_small(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_item("old", "s", "t", "x" * 500, time.time())
    store.db.execute("UPDATE items SET fetched_at=?", (time.time() - 30 * 86400,))
    store.db.commit()
    store.add_item("new", "s", "t", "", time.time())
    store.prune(days=14)
    assert [r["url"] for r in store.db.execute("SELECT url FROM items")] == ["new"]


def test_llm_outage_keeps_news_for_next_run(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "Báo Khánh Hòa", "Du khách Nga bị giật túi ở Nha Trang", "", time.time())
    monkeypatch.setattr(main, "fetch_article", lambda url: Article("Nội dung " * 60, "", None))

    def down(*a, **k):
        raise llm.LLMError("429")

    monkeypatch.setattr(llm, "score", down)
    assert main.process(store, Telegram("x")) is False
    assert store.get(1)["status"] == "candidate"
