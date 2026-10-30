"""Связь с помощником на Cloudflare (папка worker/).

Помощник мгновенно принимает нажатия кнопок и сам публикует посты в канал. Бот при каждом запуске
отдаёт ему настройки и готовые посты, а забирает события: что опубликовано, что пропущено,
где нужен полный перевод. Если WORKER_URL не задан, бот работает по-старому, без помощника."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import CFG

log = logging.getLogger(__name__)


class HelperError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(CFG.worker_url and CFG.worker_secret)


def _call(body: dict) -> dict:
    url = CFG.worker_url.rstrip("/") + "/api/sync"
    try:
        r = httpx.post(url, json=body, headers={"authorization": f"Bearer {CFG.worker_secret}"}, timeout=40)
        data = r.json()
    except (httpx.HTTPError, ValueError) as err:
        raise HelperError(f"помощник на Cloudflare не ответил: {err}") from err
    if r.status_code != 200 or not data.get("ok"):
        raise HelperError(f"помощник на Cloudflare: {r.status_code} {data.get('error', '')}")
    return data


def config_payload() -> dict:
    offset = datetime.now(ZoneInfo(CFG.timezone)).utcoffset()
    return {
        "token": CFG.telegram_token, "admin": str(CFG.admin_chat_id), "channel": str(CFG.channel_id),
        "autopublish": CFG.autopublish, "quiet_from": CFG.quiet_from, "quiet_to": CFG.quiet_to,
        "min_gap_min": CFG.min_gap_min, "max_per_day": CFG.max_posts_per_day, "max_age_h": CFG.max_age_hours,
        "tz_offset_min": int(offset.total_seconds() // 60) if offset else 420,
    }


def sync(want_events: bool = True, ack: list | None = None) -> list:
    """Отдать настройки (и подтвердить обработанные события), забрать новые события."""
    data = _call({"config": config_payload(), "want_events": want_events, "ack": ack or []})
    if data.get("webhook"):
        log.info("Помощник подключил кнопки Telegram к себе")
    if data.get("webhook_error"):
        log.warning("Кнопки к помощнику пока не подключены (повторю в следующий раз): %s", data["webhook_error"])
        print(f"::warning title=Помощник на Cloudflare::{str(data['webhook_error'])[:300]}", flush=True)
    return sorted(data.get("events") or [], key=lambda e: e.get("at", 0))


def upload(row, post_html: str, image: str, status: str, card_html: str = "", card_kind: str = "",
           card_msg_id: int = 0):
    """Отдать помощнику готовый пост: review — ждёт кнопки, queued — в очередь автопилота."""
    _call({"posts": [{
        "id": row["id"], "html": post_html, "image": image or "", "status": status,
        "card_html": card_html, "card_kind": card_kind, "card_msg_id": card_msg_id,
        "score": row["score"] or 0, "published_at": row["published_at"] or 0,
    }]})
