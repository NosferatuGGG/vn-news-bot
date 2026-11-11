"""Готовые посты: несколько изданий, проверка цифр кодом, кнопка «Опубликовать», авто-очередь.
Сеть, нейросеть и Telegram подменены заглушками."""
import time

import pytest

from bot import llm, main, money
from bot.config import CFG
from bot.fetch import Article
from bot.store import Store
from bot.telegram import Telegram, build_channel_post, visible_len
from bot.verify import check_numbers

VN_TEXT = ("Ngày 15/10, công an Khánh Hòa bắt 3 đối tượng giả mạo khách sạn ở Nha Trang, lừa 120 du khách, "
           "chiếm đoạt hơn 5,65 tỷ đồng. Một nạn nhân 45 tuổi mất 246 triệu đồng. ") * 3
VN_TEXT_2 = "Theo Thanh Niên, nhóm 3 người đã lừa 120 khách du lịch tại Nha Trang. " * 6


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(CFG, "dry_run", True)
    monkeypatch.setattr(CFG, "output", "posts")
    monkeypatch.setattr(CFG, "autopublish", False)
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    monkeypatch.setattr(CFG, "channel_id", "@novosti_vietnama")
    monkeypatch.setattr(CFG, "subscribe_url", "https://t.me/novosti_vietnama")
    monkeypatch.setattr(CFG, "min_gap_min", 0)
    monkeypatch.setattr(CFG, "quiet_from", 0)
    monkeypatch.setattr(CFG, "quiet_to", 0)
    monkeypatch.setattr(CFG, "max_writes_per_run", 5)
    monkeypatch.setattr(money, "fetch_rates", lambda store=None: {"usd_rub": 83.4839, "vnd_rub": 0.00325651})
    monkeypatch.setattr(llm, "fact_check", lambda *a: {"ok": True, "problems": []})
    monkeypatch.setattr(llm, "polish_post", lambda data, src: dict(data))   # литредактор ничего не меняет


GOOD = {"emoji": "🏨", "headline": "В Нячанге поймали фейковых «отельеров»",
        "paragraphs": ["Задержали **3 человек**: они обманули 120 туристов.",
                       "Ущерб — больше 5,6 млрд донгов, одна туристка 45 лет потеряла 246 млн донгов."]}


def _store_with(tmp_path, *rows):
    store = Store(str(tmp_path / "t.db"))
    for url, source, fields in rows:
        store.add_item(url, source, "tiêu đề", "", time.time())
        rid = store.db.execute("SELECT id FROM items WHERE url=?", (url,)).fetchone()[0]
        store.update(rid, **fields)
    return store


def _articles(monkeypatch, texts):
    monkeypatch.setattr(main, "fetch_article", lambda url: Article(texts[url], f"https://img/{url[-1]}.jpg", None))


# ---------- Проверка цифр ----------
def test_numbers_found_in_vietnamese_text():
    src = "Ngày 15/10 bắt 12 đối tượng, dòng tiền hơn 6.000 tỷ đồng, nạn nhân 45 tuổi, lãi 1.022%/năm, lúc 14h30, hai người."
    post = "15 октября задержали 12 человек, оборот больше 6 трлн донгов. Жертве 45 лет, ставка 1022%. В 14:30 ранены 2."
    assert check_numbers(post, src) == []


def test_rounding_is_allowed_but_invented_numbers_are_caught():
    src = "Thiệt hại 5,65 tỷ đồng, nạn nhân 45 tuổi."
    assert check_numbers("Ущерб больше 5,6 млрд донгов", src) == []
    problems = check_numbers("Жертве 47 лет, ущерб 300 млн донгов", src)
    assert problems == ["цифры «47» нет в статьях", "цифры «300 млн» нет в статьях"]


def test_english_sources_work_too():
    assert check_numbers("Штраф 2,5 млн долларов, 1 200 туристов", "Fined $2.5 million, over 1,200 tourists") == []


# ---------- Сборка поста ----------
def test_post_layout_signature_and_bold():
    post = build_channel_post({"emoji": "🏨", "headline": "Заголовок <x>",
                               "paragraphs": ["Первый **12 человек** & ко", "Второй"]})
    assert post.startswith("<b>🏨 Заголовок &lt;x&gt;</b>\n\n")
    assert "<b>12 человек</b> &amp; ко" in post
    assert post.endswith('<a href="https://t.me/novosti_vietnama">Подписаться на Вьетнам новости</a>')
    assert visible_len("<b>a&amp;b</b>") == 3


# ---------- Конвейер ----------
def test_two_sources_checked_and_sent_with_publish_button(tmp_path, monkeypatch):
    store = _store_with(tmp_path,
                        ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 9, "category": "scam", "heavy": 0}),
                        ("https://tn.vn/b", "Thanh Niên", {"status": "duplicate", "dup_of": 1}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT, "https://tn.vn/b": VN_TEXT_2})
    seen = []

    def write(articles, fix_notes=""):
        seen.append([a["source"] for a in articles])
        return dict(GOOD)

    monkeypatch.setattr(llm, "write_channel_post", write)
    cards = []
    tg = Telegram("x")
    monkeypatch.setattr(tg, "send_card", lambda chat, card, image, buttons, silent=False: cards.append((card, buttons)) or (7, "photo"))
    main.write_posts(store, tg, None)

    assert seen == [["VnExpress", "Thanh Niên"]]                      # оба издания ушли в нейросеть
    row = store.get(1)
    assert row["status"] == "sent" and row["card_html"]
    assert "246 млн донгов (≈ $9 600 ≈ 801 000 ₽)" in row["post_html"]   # пересчёт сделала программа
    assert "— — —" not in row["post_html"] and "Подписаться на Вьетнам новости" in row["post_html"]
    card, buttons = cards[0]
    assert "✅ <b>Проверка пройдена</b>" in card and "сверено по 2 изданиям" in card
    assert buttons[0][0]["callback_data"] == "go:1"


def test_invented_number_is_rewritten_then_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "autopublish", True)
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 8, "heavy": 0}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT})
    calls = []

    def write(articles, fix_notes=""):
        calls.append(fix_notes)
        bad = dict(GOOD)
        bad["paragraphs"] = ["Задержали 4 человек."]
        return bad

    monkeypatch.setattr(llm, "write_channel_post", write)
    tg = Telegram("x")
    cards = []
    monkeypatch.setattr(tg, "send_card", lambda chat, card, *a, **k: cards.append(card) or (7, "text"))
    main.write_posts(store, tg, None)

    assert len(calls) == 2 and "«4»" in calls[1]                       # второй раз — с подсказкой, что исправить
    assert store.get(1)["status"] == "sent"                             # в авто-режиме — не в канал, а вам
    assert "⚠️ <b>Проверка не пройдена:</b>" in cards[0] and "«4»" in cards[0]


def test_auto_mode_queues_clean_post_and_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(CFG, "autopublish", True)
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 8, "heavy": 0}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT})
    monkeypatch.setattr(llm, "write_channel_post", lambda articles, fix_notes="": dict(GOOD))
    tg = Telegram("x")
    main.write_posts(store, tg, None)
    assert store.get(1)["status"] == "queued"
    sent = []
    monkeypatch.setattr(tg, "send_post", lambda chat, text, image="": sent.append((chat, text)) or 99)
    main.publish_from_queue(store, tg)
    assert store.get(1)["status"] == "posted" and sent[0][0] == "@novosti_vietnama"


def test_heavy_story_waits_for_second_source(tmp_path, monkeypatch):
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 9, "heavy": 1}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT})
    monkeypatch.setattr(llm, "write_channel_post", lambda articles, fix_notes="": dict(GOOD))
    tg = Telegram("x")
    cards = []
    monkeypatch.setattr(tg, "send_card", lambda chat, card, *a, **k: cards.append(card) or (7, "text"))
    main.write_posts(store, tg, None)
    assert store.get(1)["status"] == "scored" and not cards           # ждёт второе издание

    store.update(1, fetched_at=time.time() - 4 * 3600)                  # прошло больше 3 часов
    main.write_posts(store, tg, None)
    assert "только одно издание" in cards[0]


def test_publish_button(tmp_path, monkeypatch):
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress",
                                   {"status": "sent", "post_html": "<b>пост</b>", "card_html": "<b>пост</b>\n\n— — —\n✅",
                                    "card_kind": "photo", "image": "https://img/a.jpg"}))
    tg = Telegram("x")
    sent, edits, toasts = [], [], []
    monkeypatch.setattr(tg, "send_post", lambda chat, text, image="": sent.append(text) or 99)
    monkeypatch.setattr(tg, "edit_card", lambda *a: edits.append(a))
    monkeypatch.setattr(tg, "answer_callback", lambda cb, text: toasts.append(text))
    upd = {"update_id": 1, "callback_query": {"id": "c", "data": "go:1", "from": {"id": 42},
                                               "message": {"message_id": 5, "chat": {"id": 42}, "photo": []}}}
    main._handle_one(store, tg, upd)
    assert sent == ["<b>пост</b>"] and store.get(1)["status"] == "posted"
    assert "В КАНАЛЕ" in edits[0][3] and toasts == ["Опубликовано в канале"]
    main._handle_one(store, tg, upd)                                     # повторное нажатие не дублирует пост
    assert len(sent) == 1 and toasts[-1] == "Уже в канале"


# ---------- Помощник на Cloudflare ----------
def _helper(monkeypatch, events=()):
    from bot import helper
    monkeypatch.setattr(CFG, "worker_url", "https://helper.example.workers.dev")
    monkeypatch.setattr(CFG, "worker_secret", "s")
    calls = []

    def fake_call(body):
        calls.append(body)
        return {"ok": True, "events": list(events) if body.get("want_events") else []}

    monkeypatch.setattr(helper, "_call", fake_call)
    return calls


def test_card_is_handed_to_helper(tmp_path, monkeypatch):
    calls = _helper(monkeypatch)
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 8, "heavy": 0}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT})
    monkeypatch.setattr(llm, "write_channel_post", lambda articles, fix_notes="": dict(GOOD))
    tg = Telegram("x")
    monkeypatch.setattr(tg, "send_card", lambda chat, card, *a, **k: (77, "photo"))
    main.write_posts(store, tg, None)
    post = calls[-1]["posts"][0]
    assert post["status"] == "review" and post["card_msg_id"] == 77 and post["card_kind"] == "photo"
    assert post["html"] == store.get(1)["post_html"] and "— — —" in post["card_html"]


def test_autopilot_queue_goes_to_helper(tmp_path, monkeypatch):
    calls = _helper(monkeypatch)
    monkeypatch.setattr(CFG, "autopublish", True)
    store = _store_with(tmp_path, ("https://vne.vn/a", "VnExpress", {"status": "scored", "score": 8, "heavy": 0}))
    _articles(monkeypatch, {"https://vne.vn/a": VN_TEXT})
    monkeypatch.setattr(llm, "write_channel_post", lambda articles, fix_notes="": dict(GOOD))
    main.write_posts(store, Telegram("x"), None)
    assert calls[-1]["posts"][0]["status"] == "queued" and store.get(1)["status"] == "queued"


def test_helper_events_update_base_and_translation_runs_here(tmp_path, monkeypatch):
    events = [{"key": "ev:1-1-posted", "action": "posted", "id": 1, "at": 1000.0},
              {"key": "ev:2-2-full", "action": "full", "id": 2, "at": 1001.0}]
    calls = _helper(monkeypatch, events)
    store = _store_with(tmp_path,
                        ("https://vne.vn/a", "VnExpress", {"status": "sent", "card_html": "c", "tg_message_id": 5}),
                        ("https://vne.vn/b", "VnExpress", {"status": "sent", "card_html": "c", "tg_message_id": 6}))
    monkeypatch.setattr(main, "fetch_article", lambda url: Article(VN_TEXT, "", None))
    monkeypatch.setattr(llm, "translate", lambda text: "перевод")
    tg = Telegram("x")
    replies = []
    monkeypatch.setattr(tg, "reply_long", lambda chat, reply_to, title, text: replies.append((reply_to, text)))
    main.apply_helper_events(store, tg)
    assert store.get(1)["status"] == "posted" and store.get(1)["posted_at"] == 1000.0
    assert replies == [(6, "перевод")]
    assert calls[-1]["ack"] == ["ev:1-1-posted", "ev:2-2-full"]
    assert calls[0]["config"]["channel"] == "@novosti_vietnama" and calls[0]["config"]["tz_offset_min"] == 420


def test_helper_fallback_go_is_scheduled_by_helper(tmp_path, monkeypatch):
    """Помощник не нашёл пост → бот передаёт его помощнику на расписание; если помощник уже поставил — бот молчит."""
    events = [{"key": "ev:1-1-go", "action": "go", "id": 1, "at": 1.0},
              {"key": "ev:2-2-go", "action": "go", "id": 2, "at": 2.0},
              {"key": "ev:3-2-scheduled", "action": "scheduled", "id": 2, "at": 3.0, "slot": 3600}]
    calls = _helper(monkeypatch, events)
    store = _store_with(tmp_path,
                        ("https://vne.vn/a", "VnExpress", {"status": "sent", "post_html": "<b>1</b>", "card_html": "c1",
                                                           "card_kind": "photo", "tg_message_id": 5}),
                        ("https://vne.vn/b", "VnExpress", {"status": "sent", "post_html": "<b>2</b>", "card_html": "c2",
                                                           "card_kind": "photo", "tg_message_id": 6}))
    tg = Telegram("x")
    sent = []
    monkeypatch.setattr(tg, "send_post", lambda chat, text, image="": sent.append(text) or 99)
    main.apply_helper_events(store, tg)
    assert sent == []                                                    # сам бот в канал не шлёт
    asked = [c["posts"][0] for c in calls if c.get("posts")]
    assert [(p["id"], p["status"], p["card_msg_id"]) for p in asked] == [(1, "schedule", 5)]
    assert store.get(2)["status"] == "scheduled"


def test_foreign_characters_are_caught_and_never_reach_the_channel():
    from bot import verify
    assert verify.check_language("Для частичной оплаты с沒有 возврата")
    assert verify.check_language("водитель xe khách")
    assert not verify.check_language("Lexus LS 600H за $312 000, Нячанг, Кханьхоа")
    assert verify.strip_foreign("с沒有 возврата") == "с возврата"


def test_polish_is_rejected_if_it_changes_numbers(monkeypatch):
    from bot import llm, main
    data = {"emoji": "🚝", "headline": "В Далате построят 100 км электропоездов",
            "paragraphs": ["Первую ветку сделают до **2030 года**."]}
    monkeypatch.setattr(llm, "polish_post", lambda d, s: {
        "emoji": "", "headline": "В Далате проложат 100 км трамвайных линий",
        "paragraphs": ["Первую ветку построят до **2030 года**."]})
    out = main._polish(data, "")
    assert out["headline"] == "В Далате проложат 100 км трамвайных линий" and out["emoji"] == "🚝"
    monkeypatch.setattr(llm, "polish_post", lambda d, s: {
        "emoji": "", "headline": "В Далате проложат 120 км линий", "paragraphs": ["Первую ветку сделают до 2030 года."]})
    assert main._polish(data, "") is data


def test_redo_marks_old_card_and_rewrites(tmp_path, monkeypatch):
    from bot import main
    from bot.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.add_item("u1", "VnExpress", "Đà Lạt", "", time.time())
    store.update(1, status="sent", tg_message_id=420, card_kind="photo", card_html="<b>старое</b>")
    monkeypatch.setattr(main, "REDO", {"redo_test": [1]})
    monkeypatch.setattr(CFG, "dry_run", False)
    monkeypatch.setattr(CFG, "admin_chat_id", "42")
    edits = []

    class TG:
        def edit_card(self, *a, **k):
            edits.append((a, k))

    main._redo_cards(store, TG())
    main._redo_cards(store, TG())
    assert store.get(1)["status"] == "scored" and len(edits) == 1
    assert "Заменено" in edits[0][0][3] and edits[0][1]["buttons"] == []


def test_police_not_militia():
    from bot import verify
    assert verify.fix_police("Милиция задержала, к настоящему моменту стражи порядка взяли под стражу 21 человека") == \
        "Полиция задержала, к настоящему моменту полицейские взяли под стражу 21 человека"
    assert verify.fix_police("двое милиционеров и сотрудник милиции") == "двое полицейских и сотрудник полиции"
