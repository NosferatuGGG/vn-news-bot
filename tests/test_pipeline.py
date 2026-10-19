"""Проверка конвейера без сети: сайты, нейросеть и Telegram подменены заглушками.
Запуск: python -m pytest tests -q"""
import time

import pytest


from bot import fetch, llm, main  # noqa: E402
from bot.config import CFG  # noqa: E402
from bot.fetch import Article, Entry, parse_listing, parse_rss  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.telegram import Telegram, TelegramError, build_post  # noqa: E402

@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(CFG, "dry_run", True)
    monkeypatch.setattr(CFG, "mode", "auto")
    monkeypatch.setattr(CFG, "min_gap_min", 0)
    monkeypatch.setattr(CFG, "quiet_from", 0)
    monkeypatch.setattr(CFG, "quiet_to", 0)
    from bot import money
    monkeypatch.setattr(money, "fetch_rates", lambda store=None: {"usd_rub": 83.4839, "vnd_rub": 0.00325651})


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
        Entry("https://vne.vn/meh", "Trộm xe đạp ở Hà Nội", "", now, "VnExpress"),
    ]
    monkeypatch.setattr(main, "SOURCES", ["one-source"])
    monkeypatch.setattr(main, "fetch_source", lambda src: entries)
    monkeypatch.setattr(main, "fetch_article", lambda url: Article("Nội dung bài báo " * 40, "https://img/x.jpg", None))
    monkeypatch.setattr(CFG, "max_writes_per_run", 5)

    batches = []

    def fake_batch(items, recent):
        batches.append([it["id"] for it in items])
        out = {}
        by_title = {it["title"]: it["id"] for it in items}
        for it in items:
            t = it["title"]
            if "Phá đường dây" in t:
                out[it["id"]] = {"id": it["id"], "score": 9, "category": "scam", "title_ru": "Фейковые отели (дубль)",
                                 "duplicate_of": by_title["Giả resort 5 sao ở Phú Quốc để lừa đặt cọc, dòng tiền hơn 6.000 tỷ đồng"], "reason": "dup"}
            elif "Phú Quốc" in t:
                out[it["id"]] = {"id": it["id"], "score": 9, "category": "scam", "title_ru": "Фейковые отели Фукуока", "duplicate_of": 0, "reason": "ok"}
            elif "du khách" in t:
                out[it["id"]] = {"id": it["id"], "score": 10, "category": "russians", "title_ru": "У туристов вырвали сумку", "duplicate_of": 0, "reason": "ok"}
            else:
                out[it["id"]] = {"id": it["id"], "score": 3, "category": "crime", "title_ru": "Кража велосипеда", "duplicate_of": 0, "reason": "скучно"}
        return out

    monkeypatch.setattr(llm, "score_batch", fake_batch)
    monkeypatch.setattr(llm, "brief", lambda *a, **k: {
        "emoji": "🚨", "headline": "Заголовок", "place": "Нячанг", "summary": "Суть", "facts": ["факт <1>"],
        "numbers": ["оборот 6000 миллиардов донгов"], "why": "рядом с нами"})
    checks = iter([{"ok": False, "problems": ["лишняя цифра"]}, {"ok": True, "problems": []}])
    monkeypatch.setattr(llm, "fact_check", lambda *a: next(checks))

    store = Store(str(tmp_path / "t.db"))
    tg = Telegram("x")
    main.collect(store)
    assert main.process(store, tg) is True

    statuses = {r["url"]: r["status"] for r in store.db.execute("SELECT url, status FROM items")}
    assert "https://tn.vn/old" not in statuses                       # старьё не берём
    assert statuses["https://bkh.vn/hoi-nghi"] == "filtered"          # скучное отсечено без нейросети
    assert statuses["https://vne.vn/ga"] == "filtered"                # «Ngã ba» ≠ «Nga»
    assert statuses["https://vne.vn/meh"] == "low"
    assert statuses["https://bkh.vn/giat-tui"] == "sent"
    assert statuses["https://vne.vn/phu-quoc"] == "sent"
    assert statuses["https://tn.vn/phu-quoc"] == "duplicate"          # дубль с другого сайта
    assert len(batches) == 1 and len(batches[0]) == 4                 # все оценены ОДНИМ запросом

    cards = {r["score"]: r["post_html"] for r in store.by_status("sent")}
    assert "<b>Суть:</b> Суть" in cards[10] and "• факт &lt;1&gt;" in cards[10] and "Оригинал:" in cards[10]
    assert "Сверь с оригиналом: лишняя цифра" in cards[10]           # первой готовится самая интересная
    assert "Сверь" not in cards[9]
    assert "• оборот 6 трлн донгов (≈ $234 млн ≈ 19,5 млрд ₽)" in cards[9]


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
    store.update(1, status="sent", post_html="<b>пост</b>", card_kind="photo", tg_message_id=5)
    tg = Telegram("x")
    edits = []
    monkeypatch.setattr(tg, "edit_card", lambda *a, **k: edits.append(a))
    upd = [{"update_id": 7, "callback_query": {"id": "c", "data": "pub:1", "from": {"id": 42},
                                               "message": {"chat": {"id": 42}, "message_id": 5}}}]
    monkeypatch.setattr(tg, "get_updates", lambda offset, timeout: upd)

    def too_old(*a, **k):
        raise RuntimeError("query is too old")

    monkeypatch.setattr(tg, "answer_callback", too_old)
    monkeypatch.setattr(tg, "edit_buttons", too_old)
    main.handle_updates(store, tg, timeout=0)
    assert store.get(1)["status"] == "published"
    assert edits and edits[0][2] == "photo" and edits[0][3].startswith("✅ <b>ОПУБЛИКОВАНО</b>")
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

    def down(*a, **k):
        raise llm.LLMError("500")

    monkeypatch.setattr(llm, "score_batch", down)
    assert main.process(store, Telegram("x")) is False
    assert store.get(1)["status"] == "candidate"


def test_daily_quota_is_not_a_failure(tmp_path, monkeypatch):
    """Кончился бесплатный лимит — не ошибка: новости ждут, запуск зелёный, тебе одно сообщение в день."""
    monkeypatch.setattr(CFG, "dry_run", False)
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "Báo Khánh Hòa", "Du khách Nga bị giật túi ở Nha Trang", "", time.time())
    sent = []
    tg = Telegram("x")
    monkeypatch.setattr(tg, "call_text", lambda chat, text: sent.append(text))
    monkeypatch.setattr(tg, "get_updates", lambda offset, timeout: [])

    def exhausted(*a, **k):
        for m in (CFG.model_fast, CFG.model_writer):
            llm.EXHAUSTED[m] = llm.quota_day()
        raise llm.LLMQuotaError("дневной лимит")

    monkeypatch.setattr(llm, "score_batch", exhausted)
    assert main.process(store, tg) is True
    assert main.process(store, tg) is True
    assert store.get(1)["status"] == "candidate"
    assert len(sent) == 1 and "лимит" in sent[0]


def test_buttons_checked_during_long_run(tmp_path, monkeypatch):
    """Нажатие «Сейчас», сделанное пока бот пишет черновики, обрабатывается в том же запуске."""
    monkeypatch.setattr(CFG, "dry_run", False)
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    monkeypatch.setattr(CFG, "max_writes_per_run", 3)
    store = Store(str(tmp_path / "t.db"))
    for i in range(3):
        store.add_item(f"u{i}", "s", "Du khách Nga bị cướp", "", time.time())
        store.update(i + 1, status="scored", score=8)
    monkeypatch.setattr(main, "fetch_article", lambda url: Article("x", "", None))  # короткий текст → без нейросети
    monkeypatch.setattr(llm, "score_batch", lambda items, recent: {})
    calls = []
    monkeypatch.setattr(main, "handle_updates", lambda store, tg, timeout: calls.append(timeout))
    main.process(store, Telegram("x"))
    assert len(calls) == 3


def _press(store, tg, monkeypatch, data, update_id=9):
    upd = [{"update_id": update_id, "callback_query": {"id": "c", "data": data, "from": {"id": 42},
                                                       "message": {"chat": {"id": 42}, "message_id": 5}}}]
    monkeypatch.setattr(tg, "get_updates", lambda offset, timeout: upd)
    main.handle_updates(store, tg, timeout=0)


def test_publish_mark_and_undo(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "s", "t", "", time.time())
    store.update(1, status="sent", post_html="КАРТОЧКА", card_kind="text", image="img", title_ru="Новость")
    tg = Telegram("x")
    edits = []
    monkeypatch.setattr(tg, "edit_card", lambda chat, mid, kind, text, image, buttons: edits.append((kind, text, buttons)))
    monkeypatch.setattr(tg, "answer_callback", lambda *a: None)
    _press(store, tg, monkeypatch, "pub:1")
    assert store.get(1)["status"] == "published" and edits[-1][1].startswith("✅")
    assert edits[-1][2][0][0]["callback_data"] == "unpub:1"
    _press(store, tg, monkeypatch, "unpub:1", 10)
    assert store.get(1)["status"] == "sent" and edits[-1][1] == "КАРТОЧКА"     # отметка снята, карточка как была
    _press(store, tg, monkeypatch, "skip:1", 11)
    assert store.get(1)["status"] == "skipped" and edits[-1][1].startswith("🚫")


def test_full_translation_replies_to_card(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "s", "t", "", time.time())
    store.update(1, status="sent", post_html="КАРТОЧКА", title_ru="Новость")
    tg = Telegram("x")
    monkeypatch.setattr(tg, "answer_callback", lambda *a: None)
    monkeypatch.setattr(main, "fetch_article", lambda url: Article("Bài báo " * 50, "", None))
    monkeypatch.setattr(llm, "translate", lambda text: "Полный перевод статьи")
    replies = []
    monkeypatch.setattr(tg, "reply_long", lambda chat, reply_to, title, text: replies.append((reply_to, text)))
    _press(store, tg, monkeypatch, "full:1")
    assert replies == [(5, "Полный перевод статьи")] and store.get(1)["status"] == "sent"


def test_failed_button_tells_you(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "s", "t", "", time.time())
    store.update(1, status="sent", post_html="КАРТОЧКА", title_ru="Новость")
    tg = Telegram("x")
    monkeypatch.setattr(tg, "edit_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("message not found")))
    told = []
    monkeypatch.setattr(tg, "call_text", lambda chat, text: told.append(text))
    _press(store, tg, monkeypatch, "pub:1")
    assert told and "message not found" in told[0]


def test_card_layout_and_long_card_goes_as_text(monkeypatch):
    from bot.telegram import build_card
    monkeypatch.setattr(CFG, "dry_run", False)
    card = build_card({"emoji": "🚨", "headline": "Сумку вырвали", "place": "Нячанг", "summary": "Суть.",
                       "facts": ["8 октября, 13:30"], "numbers": [], "why": "рядом"}, 9, "crime", "Báo Khánh Hòa", "https://x/a?b=1&c=2")
    assert card.startswith("🚨 <b>9/10 · Нячанг · криминал</b>") and "Цифры" not in card and "&amp;c=2" in card
    tg = Telegram("x")
    sent = []
    monkeypatch.setattr(tg, "_call", lambda method, **p: sent.append((method, p)) or {"message_id": 7})
    assert tg.send_card("42", "x" * 2000, "https://img/1.jpg", []) == (7, "text")
    assert sent[0][0] == "sendMessage" and sent[0][1]["link_preview_options"]["url"] == "https://img/1.jpg"
    sent.clear()
    tg.reply_long("42", 5, "Новость", "\n".join(["абзац " * 100] * 20))
    assert len(sent) > 1 and sent[0][1]["reply_parameters"]["message_id"] == 5
    assert all(len(p["text"]) <= 4096 for _, p in sent)


def test_google_overload_is_not_an_alarm_until_three_in_a_row(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "Báo Khánh Hòa", "Du khách Nga bị giật túi ở Nha Trang", "", time.time())

    def busy(*a, **k):
        raise llm.LLMBusyError("503 high demand")

    monkeypatch.setattr(llm, "score_batch", busy)
    results = [main.process(store, Telegram("x")) for _ in range(3)]
    assert results == [True, True, False] and store.get(1)["status"] == "candidate"
    monkeypatch.setattr(llm, "score_batch", lambda items, recent: {})
    assert main.process(store, Telegram("x")) is True and store.kv_get("busy_streak") == "0"


def test_image_urls_are_cleaned():
    from bot.fetch import clean_image_url
    page = "https://tuoitre.vn/a/b.htm"
    assert clean_image_url("/img/x.jpg", page) == "https://tuoitre.vn/img/x.jpg"
    assert clean_image_url("https://cdn.vn/a b.jpg", page) == "https://cdn.vn/a%20b.jpg"
    assert clean_image_url("", page) == ""


def test_card_with_bad_preview_is_resent_without_picture(monkeypatch):
    tg = Telegram("x")
    sent = []

    def fake_call(method, **kw):
        sent.append(kw["link_preview_options"])
        if kw["link_preview_options"].get("url"):
            raise TelegramError("Telegram sendMessage: {'error_code': 400, 'description': 'Bad Request: WEBPAGE_URL_INVALID'}")
        return {"message_id": 5}

    monkeypatch.setattr(tg, "_call", fake_call)
    assert tg.send_card("1", "x" * 2000, "https://bad/img.jpg") == (5, "text")
    assert sent[-1] == {"is_disabled": True}
