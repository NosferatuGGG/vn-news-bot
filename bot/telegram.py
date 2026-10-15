"""Минимальный клиент Telegram Bot API (без сторонних библиотек)."""
import html
import json
import logging

import httpx

from .config import CFG

log = logging.getLogger(__name__)
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def build_post(data: dict, url: str, source: str) -> str:
    """Собираем HTML-пост сами — модель отдаёт чистый текст, поэтому разметка не ломается."""
    e = html.escape
    parts = [f"<b>{e(data['emoji'])} {e(data['headline'])}</b>", e(data["body"].strip())]
    if data.get("takeaway", "").strip():
        parts.append(f"👉 <i>{e(data['takeaway'].strip())}</i>")
    footer = f'<a href="{e(url, quote=True)}">Источник: {e(source)}</a>'
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in data.get("hashtags", []))
    if tags:
        footer += f"\n{e(tags)}"
    if CFG.channel_signature:
        footer += f"\n{e(CFG.channel_signature)}"
    parts.append(footer)
    return "\n\n".join(parts)


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.http = httpx.Client(timeout=60)

    def _call(self, method: str, **params):
        if CFG.dry_run:
            log.info("[DRY RUN] %s %s", method, {k: v for k, v in params.items() if k != "reply_markup"})
            print(f"\n===== {method} → {params.get('chat_id')} =====\n{params.get('caption') or params.get('text')}\n")
            return {"message_id": 0}
        r = self.http.post(f"{self.base}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram {method}: {data}")
        return data["result"]

    def send_post(self, chat_id: str, text: str, image: str = "", buttons=None) -> int:
        markup = {"inline_keyboard": buttons} if buttons else None
        extra = {"reply_markup": markup} if markup else {}
        if image and len(text) <= CAPTION_LIMIT:
            try:
                res = self._call("sendPhoto", chat_id=chat_id, photo=image, caption=text, parse_mode="HTML", **extra)
                return res["message_id"]
            except Exception as err:  # noqa: BLE001 — картинку не приняли, шлём текстом
                log.warning("Фото не отправилось (%s), шлю текстом", err)
        res = self._call(
            "sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT], parse_mode="HTML",
            link_preview_options={"is_disabled": False, "prefer_large_media": True}, **extra,
        )
        return res["message_id"]

    def call_text(self, chat_id: str, text: str):
        """Простое служебное сообщение без разметки."""
        return self._call("sendMessage", chat_id=chat_id, text=text)

    def get_updates(self, offset: int, timeout: int = 25) -> list:
        if CFG.dry_run:
            return []
        r = self.http.get(
            f"{self.base}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": json.dumps(["callback_query"])},
            timeout=timeout + 10,
        )
        return r.json().get("result", [])

    def answer_callback(self, cb_id: str, text: str):
        self._call("answerCallbackQuery", callback_query_id=cb_id, text=text)

    def edit_buttons(self, chat_id, message_id, text_button: str):
        self._call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": [[{"text": text_button, "callback_data": "noop"}]]},
        )
