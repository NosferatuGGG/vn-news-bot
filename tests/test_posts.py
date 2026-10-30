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
