"""Главный цикл: собрать → отфильтровать → оценить → переписать → проверить → опубликовать.

Запуск:
  python -m bot.main                  — работать постоянно
  python -m bot.main --once           — один проход и выход (удобно для cron)
  python -m bot.main --test-url URL   — прогнать одну статью и показать готовый пост
"""
import argparse
import html
import json
import os
import logging
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import llm, money, verify
from .config import CFG
from .fetch import fetch_article, fetch_source
from .filters import prefilter
from .sources import SOURCES
from .store import Store
from .telegram import (CAPTION_LIMIT, Telegram, TelegramError, build_card, build_channel_post, build_post,
                       build_post_card, card_buttons, marked, post_buttons, visible_len)

log = logging.getLogger("bot")


# ---------- 1. Сбор ----------
def collect(store: Store) -> int:
    new = 0
    cutoff = time.time() - CFG.max_age_hours * 3600
    for src in SOURCES:
        for e in fetch_source(src):
            if e.published_at < cutoff:
                continue
            if store.add_item(e.url, e.source, e.title, e.summary, e.published_at):
                new += 1
    log.info("Собрано новых заголовков: %d", new)
    return new


# ---------- 2–4. Отбор, рерайт, проверка ----------
def make_post(title: str, text: str, source: str, url: str) -> tuple[str, list[str]]:
    data = llm.write_post(title, text, source, url, rates=CFG.rates)
    plain = f"{data['headline']}\n{data['body']}\n{data.get('takeaway', '')}"
    check = llm.fact_check(plain, text)
    if not check["ok"]:
        log.info("Фактчек нашёл проблемы, переписываю: %s", check["problems"])
        data = llm.write_post(title, text, source, url, fix_notes="; ".join(check["problems"]), rates=CFG.rates)
        plain = f"{data['headline']}\n{data['body']}\n{data.get('takeaway', '')}"
        check = llm.fact_check(plain, text)
    return build_post(data, url, source), ([] if check["ok"] else check["problems"])


def _load_quota_state(store: Store):
    llm.EXHAUSTED.clear()
    raw = store.kv_get("llm_exhausted", "")
    if raw:
        try:
            llm.EXHAUSTED.update(json.loads(raw))
        except ValueError:
            pass


def _save_quota_state(store: Store, tg: Telegram | None):
    store.kv_set("llm_exhausted", json.dumps(llm.EXHAUSTED))
    today = llm.quota_day()
    models = {CFG.model_fast, CFG.model_writer}
    if all(llm.EXHAUSTED.get(m) == today for m in models) and store.kv_get("quota_notice") != today:
        store.kv_set("quota_notice", today)
        log.warning("Бесплатный лимит нейросети на сегодня исчерпан")
        if tg and CFG.admin_chat_id and not CFG.dry_run:
            try:
                tg.call_text(CFG.admin_chat_id,
                             "ℹ️ Бесплатный лимит нейросети на сегодня закончился. "
                             "Новые черновики появятся после 14:00 по Вьетнаму, когда Google обнулит лимит. "
                             "Кнопки под уже присланными черновиками работают как обычно.")
            except Exception as err:  # noqa: BLE001
                log.warning("Не смог сообщить о лимите: %s", err)


def prefilter_queue(store: Store):
    """Предфильтр по ключевым словам — бесплатно; ждущие разбора тоже перепроверяем,
    чтобы правки фильтра сразу действовали и на очередь."""
    cutoff = time.time() - CFG.max_age_hours * 3600
    for row in list(store.by_status("new", limit=2000)) + list(store.by_status("candidate", limit=2000)):
        ok, why = prefilter(row["title"], row["summary"] or "")
        if ok and row["published_at"] < cutoff:
            ok, why = False, "old"
        store.update(row["id"], status="candidate" if ok else "filtered", note=why)
    for row in store.by_status("scored", limit=1000):
        if row["published_at"] < cutoff:
            store.update(row["id"], status="skipped", note="устарела, пока ждала очереди")


def score_candidates(store: Store):
    """Шаг 1: оценить пачку новостей ОДНИМ запросом к нейросети (по заголовкам и анонсам)."""
    order = ("CASE note WHEN 'strong' THEN 0 WHEN 'local' THEN 1 WHEN 'crime' THEN 2 ELSE 3 END, "
             "published_at DESC")
    batch = store.by_status("candidate", limit=CFG.score_batch_size, order=order)
    if not batch:
        return
    log.info("Оцениваю %d новостей одним запросом (ждут оценки: %d)", len(batch),
             len(store.by_status("candidate", limit=5000)))
    items = [{"id": r["id"], "source": r["source"], "title": r["title"], "summary": r["summary"] or ""} for r in batch]
    scores = llm.score_batch(items, store.recent_posted())
    for row in batch:
        sc = scores.get(row["id"])
        if not sc:
            continue  # модель пропустила — оценим в следующий раз
        fields = dict(score=sc["score"], category=sc.get("category", "other"), title_ru=sc.get("title_ru", ""),
                      note=str(sc.get("reason", ""))[:300], heavy=1 if sc.get("heavy") else 0)
        if sc["duplicate_of"] and sc["duplicate_of"] != row["id"] and store.get(sc["duplicate_of"]):
            log.info("  [дубль] %s", fields["title_ru"])
            store.update(row["id"], status="duplicate", dup_of=sc["duplicate_of"], **fields)
        elif sc["score"] < CFG.min_score:
            log.info("  [%s/10 — мимо] %s", sc["score"], fields["title_ru"])
            store.update(row["id"], status="low", **fields)
        else:
            log.info("  [%s/10 — в работу] %s", sc["score"], fields["title_ru"])
            store.update(row["id"], status="scored", **fields)


def send_briefs(store: Store, tg: Telegram | None, deadline: float | None):
    """Шаг 2: самые интересные из оценённых — прочитать целиком, сделать выжимку на русском, прислать тебе."""
    todo = store.by_status("scored", limit=CFG.max_writes_per_run, order="score DESC, published_at DESC")
    for row in todo:
        if deadline and time.time() > deadline:
            log.info("Время запуска вышло — остальное в следующий раз")
            return
        check_buttons(store, tg)   # нажатия, сделанные пока идёт разбор, срабатывают сразу
        started = time.time()
        try:
            art = fetch_article(row["url"])
        except Exception as err:  # noqa: BLE001
            log.warning("Не открылась статья %s: %s", row["url"], err)
            store.update(row["id"], status="error", note=str(err)[:300])
            continue
        if art.published_at:
            store.update(row["id"], published_at=art.published_at)
            if art.published_at < time.time() - CFG.max_age_hours * 3600:
                store.update(row["id"], status="filtered", note="old")
                continue
        if len(art.text) < 200:
            store.update(row["id"], status="error", note="no text")
            continue
        try:
            data = llm.brief(row["title"], art.text, row["source"], row["url"])
            plain = "\n".join([data["headline"], data["summary"], *data["facts"], *data["numbers"]])
            check = llm.fact_check(plain, art.text)
            problems = [] if check.get("ok") else [str(p) for p in check.get("problems") or []]
            data = money.annotate_brief(data, money.fetch_rates(store))   # «≈ $… ≈ … ₽» считает программа
            card = build_card(data, row["score"], row["category"] or "", row["source"], row["url"], problems)
        except llm.LLMError:
            raise   # лимит, перегрузка, сбой нейросети — разбираются выше
        except Exception as err:  # noqa: BLE001
            # что-то необычное в этой конкретной новости — пропускаем её, чтобы она не стопорила остальные
            where = traceback.extract_tb(err.__traceback__)[-1]
            msg = f"{type(err).__name__}: {err} ({os.path.basename(where.filename)}:{where.lineno})"
            log.warning("Не получилась карточка %s: %s", row["url"], msg)
            print(f"::warning title=Карточка не получилась::{row['url']} — {msg[:300]}", flush=True)
            store.update(row["id"], status="error", note=msg[:300])
            continue
        msg_id, kind = 0, "text"
        if tg and CFG.admin_chat_id:
            try:
                msg_id, kind = tg.send_card(CFG.admin_chat_id, card, art.image, card_buttons(row["id"], "sent"),
                                            silent=row["score"] < CFG.loud_score or is_quiet_time())
            except TelegramError as err:
                if "400" not in str(err):
                    raise   # Telegram недоступен целиком — это настоящий сбой
                # Telegram не принял именно эту карточку — пропускаем её, чтобы она не застопорила остальные
                log.warning("Telegram не принял карточку %s: %s", row["url"], err)
                print(f"::warning title=Карточка не отправилась::{row['url']} — {str(err)[:200]}", flush=True)
                store.update(row["id"], status="error", note=f"telegram: {str(err)[:250]}")
                continue
        # статус меняем только после успешной отправки — иначе карточка потеряется
        store.update(row["id"], status="sent", post_html=card, image=art.image, tg_message_id=msg_id,
                     card_kind=kind, title_ru=data["headline"])
        log.info("  [%s/10 — ПРИСЛАЛ] %s (%.0f с)", row["score"], data["headline"], time.time() - started)


# ---------- 3–4. Готовый пост: несколько изданий, проверка цифр кодом и редактором ----------
def _check_post(data: dict, src_text: str) -> list:
    plain = "\n".join([data["headline"], *data["paragraphs"]])
    if not data["paragraphs"]:
        return ["нейросеть вернула пустой пост"]
    problems = verify.check_numbers(plain, src_text)          # бесплатно и без ошибок нейросети
    check = llm.fact_check(plain, src_text)                    # додуманные детали, «нож» вместо «оружия» и т. п.
    if not check.get("ok"):
        problems += [str(p) for p in check.get("problems") or [] if str(p).strip()] or ["редактор нашёл неточность"]
    return problems


def make_channel_post(articles: list, heavy: bool, rates: dict) -> tuple[str, list, str]:
    """Пишет пост, проверяет, при ошибках переписывает один раз. Возвращает (HTML поста, проблемы, пометка)."""
    src_text = llm.articles_text(articles)
    data = llm.write_channel_post(articles)
    problems = _check_post(data, src_text)
    if problems:
        log.info("Проверка нашла проблемы, переписываю: %s", problems)
        data = llm.write_channel_post(articles, fix_notes="\n".join(f"- {p}" for p in problems))
        problems = _check_post(data, src_text)
    data["headline"] = money.annotate(data["headline"], rates)
    data["paragraphs"] = [money.annotate(p, rates) for p in data["paragraphs"]]   # «≈ $… ≈ … ₽» считает программа
    post = build_channel_post(data)
    if heavy and len(articles) < 2:
        problems.append("тяжёлая тема, а написало об этом только одно издание")
    notes = [f"сверено по {len(articles)} изданиям" if len(articles) > 1 else "одно издание"]
    if visible_len(post) > CAPTION_LIMIT:
        notes.append("длинный — уйдёт текстом с картинкой сверху, а не подписью к фото")
    return post, problems, "; ".join(notes)


def _ready(store: Store, row) -> bool:
    """Тяжёлая новость ждёт второе издание несколько часов; остальные — сразу."""
    if not row["heavy"] or store.companions(row["id"]):
        return True
    return time.time() - (row["fetched_at"] or 0) > CFG.hold_heavy_hours * 3600


def _article(art, row) -> dict:
    return {"source": row["source"], "url": row["url"], "title": row["title"], "text": art.text}


def write_posts(store: Store, tg: Telegram | None, deadline: float | None):
    """Шаг 2: самые интересные из оценённых — готовый пост. Проверенный в авто-режиме идёт в очередь канала,
    остальное — вам карточкой с кнопкой «Опубликовать»."""
    pool = store.by_status("scored", limit=60, order="score DESC, published_at DESC")
    todo = [r for r in pool if _ready(store, r)][:CFG.max_writes_per_run]
    rates = money.fetch_rates(store)
    for row in todo:
        if deadline and time.time() > deadline:
            log.info("Время запуска вышло — остальное в следующий раз")
            return
        check_buttons(store, tg)
        started = time.time()
        try:
            art = fetch_article(row["url"])
        except Exception as err:  # noqa: BLE001
            log.warning("Не открылась статья %s: %s", row["url"], err)
            store.update(row["id"], status="error", note=str(err)[:300])
            continue
        if art.published_at:
            store.update(row["id"], published_at=art.published_at)
            if art.published_at < time.time() - CFG.max_age_hours * 3600:
                store.update(row["id"], status="filtered", note="old")
                continue
        if len(art.text) < 200:
            store.update(row["id"], status="error", note="no text")
            continue
        articles, image = [_article(art, row)], art.image
        for comp in store.companions(row["id"])[:2]:   # та же история в других изданиях
            try:
                extra = fetch_article(comp["url"])
            except Exception as err:  # noqa: BLE001
                log.info("Второе издание не открылось %s: %s", comp["url"], err)
                continue
            if len(extra.text) >= 200:
                articles.append(_article(extra, comp))
                image = image or extra.image
        try:
            post, problems, note = make_channel_post(articles, bool(row["heavy"]), rates)
        except llm.LLMError:
            raise
        except Exception as err:  # noqa: BLE001
            where = traceback.extract_tb(err.__traceback__)[-1]
            msg = f"{type(err).__name__}: {err} ({os.path.basename(where.filename)}:{where.lineno})"
            log.warning("Не получился пост %s: %s", row["url"], msg)
            print(f"::warning title=Пост не получился::{row['url']} — {msg[:300]}", flush=True)
            store.update(row["id"], status="error", note=msg[:300])
            continue
        if CFG.autopublish and not problems:
            store.update(row["id"], status="queued", post_html=post, image=image, note=note)
            log.info("  [%s/10 — В ОЧЕРЕДЬ] %s (%.0f с)", row["score"], row["title_ru"], time.time() - started)
            continue
        card = build_post_card(post, row["score"], row["category"] or "", articles, problems, note)
        msg_id, kind = 0, "text"
        if tg and CFG.admin_chat_id:
            try:
                msg_id, kind = tg.send_card(CFG.admin_chat_id, card, image, post_buttons(row["id"], "sent"),
                                            silent=row["score"] < CFG.loud_score or is_quiet_time())
            except TelegramError as err:
                if "400" not in str(err):
                    raise
                log.warning("Telegram не принял карточку %s: %s", row["url"], err)
                store.update(row["id"], status="error", note=f"telegram: {str(err)[:250]}")
                continue
        store.update(row["id"], status="sent", post_html=post, card_html=card, image=image, tg_message_id=msg_id,
                     card_kind=kind, note="; ".join(problems)[:300] or note)
        log.info("  [%s/10 — ПРИСЛАЛ%s] %s (%.0f с)", row["score"], " С ПОМЕТКОЙ" if problems else "",
                 row["title_ru"], time.time() - started)


def process(store: Store, tg: Telegram | None, deadline: float | None = None) -> bool:
    """Возвращает False только при настоящем сбое (не при дневном лимите — он обнуляется сам)."""
    if store.kv_get("cards_v2") != "1":
        # черновики старого формата пересылаем заново — уже карточками
        for row in store.by_status("review", limit=100) + store.by_status("queued", limit=100):
            store.update(row["id"], status="scored")
        store.kv_set("cards_v2", "1")
    if store.kv_get("rescore_v1") != "1":
        # оценки стали мягче — свежие отсеянные новости переоцениваем заново
        with store.tx() as db:
            db.execute("UPDATE items SET status='candidate' WHERE status='low' AND fetched_at>?",
                       (time.time() - 30 * 3600,))
        store.kv_set("rescore_v1", "1")
    prefilter_queue(store)
    _load_quota_state(store)
    llm.DEADLINE = deadline
    try:
        score_candidates(store)
        if CFG.output == "briefs":
            send_briefs(store, tg, deadline)
        else:
            write_posts(store, tg, deadline)
        store.kv_set("busy_streak", 0)
        return True
    except llm.LLMQuotaError as err:
        log.info("Нейросеть на паузе: %s. Продолжу в следующий запуск.", err)
        return True
    except llm.LLMBusyError as err:
        # у Google временная перегрузка — не ошибка бота; письмо шлём, только если так 3 запуска подряд
        streak = int(store.kv_get("busy_streak", 0)) + 1
        store.kv_set("busy_streak", streak)
        log.warning("Нейросеть перегружена (%s), попробую в следующий запуск. Подряд: %d", err, streak)
        print(f"::warning title=Нейросеть перегружена::{str(err)[:300]} (подряд: {streak})", flush=True)
        return streak < 3
    except llm.LLMError as err:
        log.error("Нейросеть не ответила: %s", err)
        print(f"::error title=Нейросеть::{str(err)[:900]}", flush=True)
        return False
    except TelegramError as err:
        log.error("Telegram не принял черновик: %s", err)
        print(f"::error title=Telegram::{str(err)[:900]}", flush=True)
        return False
    finally:
        llm.DEADLINE = None
        _save_quota_state(store, tg)


# ---------- 5. Публикация по расписанию ----------
def is_quiet_time() -> bool:
    """Ночь по Вьетнаму: карточки приходят без звука."""
    h = datetime.now(ZoneInfo(CFG.timezone)).hour
    return (h >= CFG.quiet_from or h < CFG.quiet_to) if CFG.quiet_from > CFG.quiet_to \
        else (CFG.quiet_from <= h < CFG.quiet_to)


def can_post_now(store: Store) -> bool:
    now = datetime.now(ZoneInfo(CFG.timezone))
    if is_quiet_time():
        return False
    if time.time() - store.last_post_time() < CFG.min_gap_min * 60:
        return False
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return store.posted_since(day_start) < CFG.max_posts_per_day


def publish(store: Store, tg: Telegram, item_id: int):
    row = store.get(item_id)
    msg_id = tg.send_post(CFG.channel_id, row["post_html"], row["image"] or "")
    store.update(item_id, status="posted", posted_at=time.time(), tg_message_id=msg_id)
    log.info("Опубликовано: %s", row["title_ru"])


def publish_from_queue(store: Store, tg: Telegram):
    # протухшее из очереди выкидываем
    for row in store.by_status("queued", limit=100):
        if row["published_at"] < time.time() - CFG.max_age_hours * 3600:
            store.update(row["id"], status="skipped", note="expired in queue")
    if not can_post_now(store):
        return
    queue = store.by_status("queued", limit=1, order="score DESC, published_at DESC")
    if queue:
        publish(store, tg, queue[0]["id"])


# ---------- Кнопки модерации ----------
def handle_updates(store: Store, tg: Telegram, timeout: int):
    offset = int(store.kv_get("tg_offset", 0))
    updates = tg.get_updates(offset, timeout)
    if updates:
        log.info("Нажатий кнопок: %d", len(updates))
    for upd in updates:
        try:
            _handle_one(store, tg, upd)
        finally:
            # отмечаем нажатие обработанным только после попытки его выполнить
            store.kv_set("tg_offset", upd["update_id"] + 1)


ACTIONS = {"pub": "published", "skip": "skipped", "unpub": "sent"}
TOASTS = {"pub": "Отмечено: опубликовано", "skip": "Пропущено", "unpub": "Отметка снята"}


def _handle_one(store: Store, tg: Telegram, upd: dict):
    cb = upd.get("callback_query")
    if not cb or ":" not in cb.get("data", ""):
        return
    if str(cb["from"]["id"]) != str(CFG.admin_chat_id) and str(cb["message"]["chat"]["id"]) != str(CFG.admin_chat_id):
        return
    action, item_id = cb["data"].split(":", 1)
    row = store.get(int(item_id)) if item_id.isdigit() else None
    msg = cb["message"]
    chat_id, message_id = msg["chat"]["id"], msg["message_id"]

    def toast(text):
        try:
            tg.answer_callback(cb["id"], text)
        except Exception as err:  # noqa: BLE001 — нажатие обработано позже, Telegram уже не ждёт ответа
            log.info("Telegram не принял ответ на кнопку: %s", err)

    if not row or action not in (*ACTIONS, "full", "go", "back"):
        toast("Эта кнопка от старой версии бота")
        return
    is_post = bool(row["card_html"])   # карточка с готовым постом (а не выжимка фактов)

    def redraw(status):
        kind = row["card_kind"] or ("photo" if "photo" in msg else "text")
        tg.edit_card(chat_id, message_id, kind, marked(row["card_html"], status), row["image"] or "",
                     post_buttons(row["id"], status))

    try:
        if action == "go":
            if row["status"] == "posted":
                toast("Уже в канале")
                return
            publish(store, tg, row["id"])
            redraw("posted")
            toast("Опубликовано в канале")
        elif is_post and action in ("skip", "back", "unpub"):
            status = "skipped" if action == "skip" else "sent"
            store.update(row["id"], status=status)
            redraw(status)
            toast("Пропущено" if status == "skipped" else "Вернул")
        elif action in ACTIONS:
            status = ACTIONS[action]
            store.update(row["id"], status=status)
            if row["post_html"]:
                kind = row["card_kind"] or ("photo" if "photo" in (msg.keys()) else "text")
                tg.edit_card(chat_id, message_id, kind, marked(row["post_html"], status), row["image"] or "",
                             card_buttons(row["id"], status))
            log.info("Кнопка «%s»: %s", TOASTS[action], row["title_ru"])
            toast(TOASTS[action])
        else:  # полный перевод
            toast("Перевожу…")
            art = fetch_article(row["url"])
            text = llm.translate(art.text)
            tg.reply_long(chat_id, message_id, row["title_ru"] or row["title"], text)
            log.info("Полный перевод: %s", row["title_ru"])
    except llm.LLMQuotaError:
        tg.call_text(CFG.admin_chat_id, "⏳ Лимит нейросети на сегодня закончился — полный перевод пришлю после 14:00 "
                                        "по Вьетнаму. Нажми кнопку ещё раз после этого времени.")
    except Exception as err:  # noqa: BLE001
        log.error("Не смог выполнить кнопку %s для %s: %s", action, row["id"], err)
        print(f"::error title=Кнопка::{str(err)[:500]}", flush=True)
        try:
            tg.call_text(CFG.admin_chat_id, f"⚠️ Кнопка не сработала для «{row['title_ru'] or row['title']}»: {str(err)[:300]}")
        except Exception:  # noqa: BLE001
            pass


# ---------- Точки входа ----------
def check_buttons(store: Store, tg: Telegram | None, timeout: int = 0):
    """Обработать нажатые кнопки модерации; сбой Telegram здесь не должен останавливать разбор."""
    if tg is None or CFG.dry_run or not CFG.admin_chat_id:
        return
    try:
        handle_updates(store, tg, timeout=timeout)
    except Exception as err:  # noqa: BLE001
        log.warning("Не удалось проверить кнопки: %s", err)
        print(f"::warning title=Кнопки::{str(err)[:300]}", flush=True)


def _notice(text: str):
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::notice title=Бот::{text}", flush=True)


def run_once(store: Store, tg: Telegram):
    """Один проход — для запуска по расписанию (GitHub Actions, cron)."""
    if not CFG.dry_run:
        try:
            info = tg.updates_info()
            _notice(f"Telegram: ждут обработки {info.get('pending_update_count', '?')}, "
                    f"принимаю {info.get('allowed_updates', 'всё')}, вебхук: {info.get('url') or 'нет'}")
        except Exception as err:  # noqa: BLE001
            _notice(f"Telegram не ответил на проверку: {str(err)[:200]}")
    check_buttons(store, tg)          # сначала нажатые тобой кнопки
    collect(store)
    # GitHub прерывает запуск через 15 минут — заканчиваем разбор заранее, чтобы успеть всё сохранить
    llm_ok = process(store, tg, deadline=time.time() + CFG.run_budget_min * 60)
    check_buttons(store, tg)          # и те, что нажаты, пока шёл разбор
    if CFG.autopublish and not CFG.dry_run:
        try:
            publish_from_queue(store, tg)   # проверенные посты — в канал по расписанию
        except TelegramError as err:
            log.error("Не смог опубликовать из очереди: %s", err)
            print(f"::error title=Публикация::{str(err)[:500]}", flush=True)
            llm_ok = False
    store.prune(days=CFG.keep_days)
    counts = dict(store.db.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
    _notice("Итог: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if not llm_ok:
        # строка ::error:: видна прямо на странице запуска в GitHub; красный запуск = письмо на почту
        print("::error title=Бот не смог доработать::Нейросеть или Telegram не ответили. "
              "Подробности — в журнале шага «Запуск бота», строки с ошибкой.", flush=True)
        raise SystemExit(1)


def run_forever(store: Store, tg: Telegram):
    next_scan = 0.0
    while True:
        try:
            if time.time() >= next_scan:
                collect(store)
                process(store, tg)
                next_scan = time.time() + CFG.scan_interval_min * 60
                store.prune(days=CFG.keep_days)
            if CFG.autopublish and not CFG.dry_run:
                publish_from_queue(store, tg)
            if CFG.admin_chat_id and not CFG.dry_run:
                handle_updates(store, tg, timeout=25)
            else:
                time.sleep(30)
        except KeyboardInterrupt:
            raise
        except Exception:  # noqa: BLE001 — бот не должен падать из-за одного сбоя
            log.exception("Сбой цикла, жду минуту")
            time.sleep(60)


def test_url(url: str):
    art = fetch_article(url)
    print(f"Текст статьи: {len(art.text)} знаков, картинка: {art.image or 'нет'}\n")
    sc = llm.score("", "", art.text, url.split('/')[2], [])
    print("ОЦЕНКА:", sc, "\n")
    articles = [{"source": url.split('/')[2], "url": url, "title": "", "text": art.text}]
    post, problems, note = make_channel_post(articles, bool(sc.get("heavy")), money.fetch_rates())
    print(post)
    print(f"\n{visible_len(post)}/1024 знаков; {note}")
    print("Проверка:", "OK" if not problems else problems)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M")
    # технические строки о каждом запросе в журнале не нужны
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-url")
    args = ap.parse_args()

    if args.test_url:
        return test_url(args.test_url)

    store = Store(CFG.db_path)
    tg = Telegram(CFG.telegram_token)
    if args.once:
        try:
            run_once(store, tg)
        except SystemExit:
            raise
        except Exception as err:  # noqa: BLE001
            # причина падения — прямо на странице запуска и в письме, без чтения журнала
            tb = traceback.extract_tb(err.__traceback__)
            where = " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(tb[-4:]))
            print(f"::error title=Бот упал::{type(err).__name__}: {str(err)[:300]} | {where}", flush=True)
            raise
    else:
        log.info("Старт. Режим: %s, источников: %d", CFG.mode, len(SOURCES))
        run_forever(store, tg)


if __name__ == "__main__":
    main()
