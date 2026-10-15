"""Главный цикл: собрать → отфильтровать → оценить → переписать → проверить → опубликовать.

Запуск:
  python -m bot.main                  — работать постоянно
  python -m bot.main --once           — один проход и выход (удобно для cron)
  python -m bot.main --test-url URL   — прогнать одну статью и показать готовый пост
"""
import argparse
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import llm
from .config import CFG
from .fetch import fetch_article, fetch_source
from .filters import prefilter
from .sources import SOURCES
from .store import Store
from .telegram import Telegram, TelegramError, build_post

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


def process(store: Store, tg: Telegram | None) -> bool:
    """Возвращает False, если нейросеть или Telegram не ответили (новости дождутся следующего запуска)."""
    # предфильтр по ключевым словам — бесплатно
    for row in store.by_status("new", limit=500):
        ok, why = prefilter(row["title"], row["summary"] or "")
        store.update(row["id"], status="candidate" if ok else "filtered", note=why)

    cutoff = time.time() - CFG.max_age_hours * 3600
    for row in store.by_status("candidate", limit=1000):
        if row["published_at"] < cutoff:
            store.update(row["id"], status="filtered", note="old")

    for row in store.by_status("candidate", limit=CFG.max_llm_items_per_scan):
        try:
            art = fetch_article(row["url"])
            if art.published_at:
                store.update(row["id"], published_at=art.published_at)
            if art.published_at and art.published_at < time.time() - CFG.max_age_hours * 3600:
                store.update(row["id"], status="filtered", note="old")
                continue
            if len(art.text) < 200:
                store.update(row["id"], status="error", note="no text")
                continue

            sc = llm.score(row["title"], row["summary"] or "", art.text, row["source"], store.recent_posted())
            fields = dict(score=sc["score"], category=sc["category"], title_ru=sc["title_ru"],
                          image=art.image, note=sc["reason"][:300])
            if sc.get("duplicate_of"):
                store.update(row["id"], status="duplicate", **fields)
                continue
            if sc["score"] < CFG.min_score:
                store.update(row["id"], status="low", **fields)
                continue

            post, problems = make_post(row["title"], art.text, row["source"], row["url"])
            log.info("Готов пост [%s] %s", sc["score"], sc["title_ru"])

            if problems or CFG.mode == "moderate":
                # спорное — всегда через человека, даже в авто-режиме
                if tg and CFG.admin_chat_id:
                    head = f"⭐ {sc['score']}/10 · {html.escape(sc['category'])}\n"
                    if problems:
                        head += "⚠️ Фактчек сомневается: " + html.escape("; ".join(problems)[:300]) + "\n"
                    tg.send_post(CFG.admin_chat_id, head + "\n" + post, art.image, buttons=[[
                        {"text": "✅ В очередь", "callback_data": f"q:{row['id']}"},
                        {"text": "🚀 Сейчас", "callback_data": f"now:{row['id']}"},
                        {"text": "❌ Мимо", "callback_data": f"skip:{row['id']}"},
                    ]])
                # статус меняем только после успешной отправки — иначе черновик потеряется
                store.update(row["id"], status="review", post_html=post, **fields)
            else:
                store.update(row["id"], status="queued", post_html=post, **fields)
        except llm.LLMError as err:
            # лимит или сбой нейросети: новость остаётся в очереди на разбор до следующего запуска
            log.error("Нейросеть недоступна: %s", err)
            return False
        except TelegramError as err:
            log.error("Telegram не принял черновик: %s", err)
            return False
        except Exception as err:  # noqa: BLE001
            log.exception("Ошибка на %s", row["url"])
            store.update(row["id"], status="error", note=str(err)[:300])
    return True


# ---------- 5. Публикация по расписанию ----------
def can_post_now(store: Store) -> bool:
    now = datetime.now(ZoneInfo(CFG.timezone))
    h = now.hour
    quiet = (h >= CFG.quiet_from or h < CFG.quiet_to) if CFG.quiet_from > CFG.quiet_to \
        else (CFG.quiet_from <= h < CFG.quiet_to)
    if quiet:
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
    for upd in tg.get_updates(offset, timeout):
        store.kv_set("tg_offset", upd["update_id"] + 1)
        cb = upd.get("callback_query")
        if not cb or ":" not in cb.get("data", ""):
            continue
        if str(cb["from"]["id"]) != str(CFG.admin_chat_id) and str(cb["message"]["chat"]["id"]) != str(CFG.admin_chat_id):
            continue
        action, item_id = cb["data"].split(":", 1)
        row = store.get(int(item_id)) if item_id.isdigit() else None
        if not row:
            continue
        msg = cb["message"]
        if action == "q":
            store.update(row["id"], status="queued")
            label, toast = "✅ В очереди", "В очереди"
        elif action == "now":
            publish(store, tg, row["id"])
            label, toast = "🚀 Опубликовано", "Опубликовано"
        elif action == "skip":
            store.update(row["id"], status="skipped")
            label, toast = "❌ Пропущено", "Пропущено"
        else:
            continue
        # при запуске по расписанию нажатие обрабатывается позже — Telegram может
        # уже не принять ответ на кнопку; само действие при этом выполнено
        for call in (lambda: tg.answer_callback(cb["id"], toast),
                     lambda: tg.edit_buttons(msg["chat"]["id"], msg["message_id"], label)):
            try:
                call()
            except Exception as err:  # noqa: BLE001
                log.info("Telegram не принял ответ на кнопку: %s", err)


# ---------- Точки входа ----------
def run_once(store: Store, tg: Telegram):
    """Один проход — для запуска по расписанию (GitHub Actions, cron)."""
    if CFG.mode == "moderate" and not CFG.dry_run:
        handle_updates(store, tg, timeout=0)   # сначала нажатые тобой кнопки
    collect(store)
    llm_ok = process(store, tg)
    publish_from_queue(store, tg)
    store.prune(days=CFG.keep_days)
    if not llm_ok:
        # красный запуск в GitHub = письмо тебе на почту
        raise SystemExit("Нейросеть или Telegram не ответили — подробности выше в журнале (строки ERROR)")


def run_forever(store: Store, tg: Telegram):
    next_scan = 0.0
    while True:
        try:
            if time.time() >= next_scan:
                collect(store)
                process(store, tg)
                next_scan = time.time() + CFG.scan_interval_min * 60
                store.prune(days=CFG.keep_days)
            publish_from_queue(store, tg)
            if CFG.mode == "moderate" and not CFG.dry_run:
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
    post, problems = make_post("", art.text, url.split('/')[2], url)
    print(post)
    print("\nФактчек:", "OK" if not problems else problems)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-url")
    args = ap.parse_args()

    if args.test_url:
        return test_url(args.test_url)

    store = Store(CFG.db_path)
    tg = Telegram(CFG.telegram_token)
    if args.once:
        run_once(store, tg)
    else:
        log.info("Старт. Режим: %s, источников: %d", CFG.mode, len(SOURCES))
        run_forever(store, tg)


if __name__ == "__main__":
    main()
