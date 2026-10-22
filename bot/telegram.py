"""Минимальный клиент Telegram Bot API (без сторонних библиотек)."""
import html
import json
import logging
import time

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


CATEGORY_RU = {
    "russians": "русские", "crime": "криминал", "scam": "мошенники", "visa_law": "визы и законы",
    "money": "деньги", "flights": "перелёты", "weather": "погода", "incident": "происшествие",
    "viral": "курьёз", "other": "разное",
}


def build_card(data: dict, score: int, category: str, source: str, url: str, problems=None) -> str:
    """Карточка новости для редактора: суть, факты, цифры, ссылка на оригинал."""
    e = html.escape
    head = " · ".join(x for x in (f"{score}/10", data.get("place", "").strip(), CATEGORY_RU.get(category, "")) if x)
    parts = [f"{e(data.get('emoji', '📰'))} <b>{e(head)}</b>\n<b>{e(data['headline'].strip())}</b>",
             f"<b>Суть:</b> {e(data['summary'].strip())}"]
    if data.get("facts"):
        parts.append("<b>Факты:</b>\n" + "\n".join(f"• {e(f)}" for f in data["facts"]))
    if data.get("numbers"):
        parts.append("<b>Цифры:</b>\n" + "\n".join(f"• {e(n)}" for n in data["numbers"]))
    if data.get("why", "").strip():
        parts.append(f"<b>Почему зайдёт:</b> {e(data['why'].strip())}")
    if problems:
        parts.append("⚠️ <i>Сверь с оригиналом: " + e("; ".join(problems)[:300]) + "</i>")
    parts.append(f'🔗 <a href="{e(url, quote=True)}">Оригинал: {e(source)}</a>')
    return "\n\n".join(parts)


MARKS = {"published": "✅ <b>ОПУБЛИКОВАНО</b>", "skipped": "🚫 <b>Пропущено</b>"}


def card_buttons(item_id: int, status: str) -> list:
    if status == "published":
        return [[{"text": "↩️ Снять отметку", "callback_data": f"unpub:{item_id}"},
                 {"text": "📄 Полный перевод", "callback_data": f"full:{item_id}"}]]
    if status == "skipped":
        return [[{"text": "↩️ Вернуть", "callback_data": f"unpub:{item_id}"}]]
    return [[{"text": "✅ Опубликовал", "callback_data": f"pub:{item_id}"}],
            [{"text": "📄 Полный перевод", "callback_data": f"full:{item_id}"},
             {"text": "🚫 Мимо", "callback_data": f"skip:{item_id}"}]]


def marked(card: str, status: str) -> str:
    return f"{MARKS[status]}\n\n{card}" if status in MARKS else card


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
        data = None
        for attempt in range(3):   # короткие обрывы связи с Telegram переживаем повтором
            try:
                r = self.http.post(f"{self.base}/{method}", json=params)
                data = r.json()
            except (httpx.TransportError, ValueError) as err:
                log.warning("Telegram %s: связь оборвалась (%s), повторяю", method, err)
                data = {"ok": False, "description": f"нет связи: {err}"}
                time.sleep(3 * (attempt + 1))
                continue
            if data.get("error_code") in (429, 500, 502, 503, 504):
                wait = (data.get("parameters") or {}).get("retry_after") or 3 * (attempt + 1)
                time.sleep(min(wait, 30))
                continue
            break
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

    def send_card(self, chat_id: str, text: str, image: str = "", buttons=None, silent=False) -> tuple:
        """Карточка с фото: короткая — подписью к фото, длинная — текстом с превью фото сверху.
        Возвращает (id сообщения, вид: photo | text)."""
        extra = {"reply_markup": {"inline_keyboard": buttons}} if buttons else {}
        if image and len(text) <= CAPTION_LIMIT:
            try:
                res = self._call("sendPhoto", chat_id=chat_id, photo=image, caption=text, parse_mode="HTML",
                                 disable_notification=silent, **extra)
                return res["message_id"], "photo"
            except TelegramError as err:
                log.warning("Фото не отправилось (%s), шлю текстом", err)
        try:
            res = self._call("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT], parse_mode="HTML",
                             disable_notification=silent, link_preview_options=self._preview(image), **extra)
        except TelegramError as err:
            if not image:
                raise
            log.warning("Telegram не принял картинку (%s), шлю без неё", err)
            res = self._call("sendMessage", chat_id=chat_id, text=text[:TEXT_LIMIT], parse_mode="HTML",
                             disable_notification=silent, link_preview_options=self._preview(""), **extra)
        return res["message_id"], "text"

    @staticmethod
    def _preview(image: str) -> dict:
        if image:
            return {"url": image, "prefer_large_media": True, "show_above_text": True}
        return {"is_disabled": True}

    def edit_card(self, chat_id, message_id: int, kind: str, text: str, image: str = "", buttons=None):
        markup = {"inline_keyboard": buttons or []}
        if kind == "photo":
            self._call("editMessageCaption", chat_id=chat_id, message_id=message_id, caption=text[:CAPTION_LIMIT],
                       parse_mode="HTML", reply_markup=markup)
        else:
            try:
                self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:TEXT_LIMIT],
                           parse_mode="HTML", link_preview_options=self._preview(image), reply_markup=markup)
            except TelegramError:
                if not image:
                    raise
                self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:TEXT_LIMIT],
                           parse_mode="HTML", link_preview_options=self._preview(""), reply_markup=markup)

    def reply_long(self, chat_id, reply_to: int, title: str, text: str):
        """Длинный текст (полный перевод) — несколькими сообщениями ответом на карточку."""
        chunks, current = [], f"📄 Полный перевод: {title}\n\n"
        paras = []
        for para in text.split("\n"):
            paras += [para[i:i + 3500] for i in range(0, len(para), 3500)] or [""]
        for para in paras:
            if len(current) + len(para) + 1 > 3900:
                chunks.append(current)
                current = ""
            current += para + "\n"
        chunks.append(current)
        for i, chunk in enumerate(chunks):
            extra = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if i == 0 else {}
            self._call("sendMessage", chat_id=chat_id, text=chunk.strip(), disable_notification=True,
                       link_preview_options={"is_disabled": True}, **extra)

    def call_text(self, chat_id: str, text: str):
        """Простое служебное сообщение без разметки."""
        return self._call("sendMessage", chat_id=chat_id, text=text)

    def get_updates(self, offset: int, timeout: int = 25) -> list:
        if CFG.dry_run:
            return []
        r = self.http.post(
            f"{self.base}/getUpdates",
            json={"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]},
            timeout=timeout + 10,
        )
        data = r.json()
        if not data.get("ok"):
            raise TelegramError(f"Telegram getUpdates: {data}")
        return data.get("result", [])

    def updates_info(self) -> dict:
        """Сколько нажатий ждёт бота в Telegram и что он принимает — для журнала."""
        if CFG.dry_run:
            return {}
        info = self._call("getWebhookInfo")
        if info.get("url"):
            # у бота включён вебхук — тогда нажатия не приходят в getUpdates; отключаем
            self._call("deleteWebhook", drop_pending_updates=False)
        return info

    def answer_callback(self, cb_id: str, text: str):
        self._call("answerCallbackQuery", callback_query_id=cb_id, text=text)

    def edit_buttons(self, chat_id, message_id, text_button: str):
        self._call(
            "editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
            reply_markup={"inline_keyboard": [[{"text": text_button, "callback_data": "noop"}]]},
        )
