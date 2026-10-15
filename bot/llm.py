"""Работа с нейросетью: оценка интересности, рерайт на русский, проверка фактов.

Поддерживаются три провайдера (переменная LLM_PROVIDER):
  gemini     — Google Gemini, есть бесплатный тариф (по умолчанию)
  openrouter — OpenRouter: один ключ ко многим моделям, оплата картой или криптой
  anthropic  — Claude напрямую

Ответ модели всегда — JSON строго по схеме, поэтому код не ломается от лишних слов."""
import json
import logging
import os
import re
import time

import httpx

from .config import CFG
from .prompts import FACTCHECK_SYSTEM, SCORE_SYSTEM, WRITE_SYSTEM

log = logging.getLogger(__name__)

CATEGORIES = ["russians", "crime", "scam", "visa_law", "money", "flights", "weather", "incident", "viral", "other"]

OPENAI_COMPAT_URLS = {
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
# Gemini вызываем через «родные» адреса Google с ключом в заголовке x-goog-api-key:
# так работают и старые ключи (AIza…), и новые ключи AI Studio (AQ.…).
# Основной адрес — Interactions API (его сейчас показывает документация Google),
# запасной — классический generateContent.
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

_http = httpx.Client(timeout=120)
_last_call: dict = {}          # модель → время последнего запроса
DEADLINE: float | None = None  # после этого момента новых запросов не делаем (выставляет main)
EXHAUSTED: dict = {}           # модель → дата (по Калифорнии), когда кончился дневной лимит


class LLMError(RuntimeError):
    pass


class LLMQuotaError(LLMError):
    """Кончился бесплатный лимит модели на сегодня (или время запуска вышло) — повторять бесполезно."""


_annotations = 0


def _annotate(level: str, title: str, text: str):
    """Пометка на странице запуска в GitHub (видна и через API) — чтобы причины сбоев были на виду."""
    global _annotations
    if os.getenv("GITHUB_ACTIONS") != "true" or _annotations >= 8:
        return
    _annotations += 1
    text = text.replace("\n", " ").replace("%", "%25")
    print(f"::{level} title={title}::{text}", flush=True)


def _interval(model: str) -> float:
    """Пауза между запросами к одной модели. У бесплатного Gemini Flash — 5 запросов в минуту,
    у Flash-Lite — 15, поэтому паузы разные."""
    if CFG.llm_provider != "gemini":
        return CFG.llm_min_interval_sec
    base = CFG.llm_min_interval_sec
    return max(base, 5.0) if "lite" in model or "gemma" in model else max(base, 13.0)


def _throttle(model: str = ""):
    wait = _interval(model) - (time.time() - _last_call.get(model, 0.0))
    if wait > 0:
        _sleep(wait)
    _last_call[model] = time.time()


def _sleep(seconds: float):
    if DEADLINE and time.time() + seconds > DEADLINE:
        raise LLMQuotaError("время запуска вышло")
    time.sleep(seconds)


def quota_day() -> str:
    """Дневные лимиты Google обнуляются в полночь по тихоокеанскому времени."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _parse_429(text: str) -> tuple[bool, float]:
    """(дневной ли это лимит, через сколько секунд советуют повторить)."""
    daily = "PerDay" in text or ("exceeded your current quota" in text and "PerMinute" not in text)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', text)
    return daily, float(m.group(1)) if m else 0.0


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise LLMError(f"Модель вернула не JSON: {text[:200]}")
        return json.loads(m.group(0))


def _post_with_retry(url: str, headers: dict, body: dict, attempts: int = 5, model: str = "") -> dict:
    for attempt in range(attempts):
        _throttle(model)
        try:
            r = _http.post(url, headers=headers, json=body)
        except httpx.TimeoutException:
            log.warning("Нейросеть не ответила вовремя, повторяю")
            continue
        if r.status_code == 429 and CFG.llm_provider == "gemini":
            daily, retry = _parse_429(r.text)
            if daily:
                raise LLMQuotaError(f"дневной лимит модели {model} исчерпан")
            log.warning("Лимит запросов в минуту у %s, жду %.0f с", model, retry or 30)
            _sleep(min(retry or 30, 60))
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            delay = float(r.headers.get("retry-after") or 0) or min(60, 5 * 2 ** attempt)
            log.warning("Нейросеть ответила %s, жду %.0f с", r.status_code, delay)
            _annotate("warning", f"Нейросеть {r.status_code}", r.text[:200])
            _sleep(delay)
            continue
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:500]}")
        return r.json()
    raise LLMError(f"нет ответа после {attempts} попыток")


def _call_openai_compat(model: str, system: str, user: str, name: str, schema: dict, max_tokens: int) -> dict:
    url = OPENAI_COMPAT_URLS[CFG.llm_provider]
    headers = {"Authorization": f"Bearer {CFG.llm_api_key}"}
    if CFG.llm_provider == "openrouter":
        headers["X-Title"] = "vn-news-bot"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system + "\n\nОтвет — только JSON-объект по схеме, без пояснений."},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
    }
    try:
        data = _post_with_retry(url, headers, body, model=model)
    except LLMError as e:
        if isinstance(e, LLMQuotaError) or not str(e).startswith("400"):
            raise
        # модель не умеет схемы — просим просто JSON и даём схему текстом
        body["response_format"] = {"type": "json_object"}
        body["messages"][0]["content"] += "\nСхема: " + json.dumps(schema, ensure_ascii=False)
        data = _post_with_retry(url, headers, body, model=model)
    content = data["choices"][0]["message"].get("content") or ""
    return _extract_json(content)


def _gemini_interactions_text(data: dict) -> str:
    steps = data.get("steps") or data.get("outputs") or []
    texts = []
    for step in steps:
        if not isinstance(step, dict) or step.get("type") not in ("model_output", None):
            continue
        content = step.get("content") or []
        if isinstance(content, dict):
            content = [content]
        texts += [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type", "text") == "text"]
        if step.get("text"):
            texts.append(step["text"])
    return "".join(texts) or data.get("output_text", "")


def _gemini_generate_text(data: dict) -> str:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason", "пустой ответ")
        raise LLMError(f"Gemini не ответил: {reason}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


GEMINI_COMPAT_URL = GEMINI_BASE + "/openai/chat/completions"
_gemini_route = None  # способ, который сработал последним, — с него и начинаем


def _call_gemini(model: str, system: str, user: str, name: str, schema: dict, max_tokens: int) -> dict:
    """Пробуем по очереди все способы, которыми Google принимает запросы, — какой-нибудь да сработает.
    Первым идёт тот, что уже сработал в этом запуске."""
    global _gemini_route
    key = CFG.llm_api_key
    instruction = system + "\n\nОтвет — только JSON-объект по схеме, без пояснений."
    with_schema_text = instruction + "\nСхема: " + json.dumps(schema, ensure_ascii=False)
    routes = {
        "compat": (GEMINI_COMPAT_URL, {"Authorization": f"Bearer {key}"}, {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": with_schema_text}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }),
        "generate": (f"{GEMINI_BASE}/models/{model}:generateContent", {"x-goog-api-key": key}, {
            "systemInstruction": {"parts": [{"text": with_schema_text}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "responseMimeType": "application/json"},
        }),
        "interactions": (f"{GEMINI_BASE}/interactions", {"x-goog-api-key": key}, {
            "model": model, "input": user, "system_instruction": with_schema_text,
            "generation_config": {"max_output_tokens": max_tokens}, "store": False,
        }),
    }
    order = ["compat", "generate", "interactions"]
    if _gemini_route in order:
        order.remove(_gemini_route)
        order.insert(0, _gemini_route)
    errors = []
    for route in order:
        url, headers, body = routes[route]
        try:
            data = _post_with_retry(url, headers, body, attempts=3, model=model)
            if route == "compat":
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            elif route == "generate":
                text = _gemini_generate_text(data)
            else:
                text = _gemini_interactions_text(data)
            result = _extract_json(text)
        except LLMQuotaError:
            raise  # лимит общий для всех способов — пробовать другие бессмысленно
        except (LLMError, ValueError, KeyError, IndexError, TypeError) as e:
            errors.append(f"{route}: {str(e)[:300]}")
            log.warning("Gemini, способ %s не сработал: %s", route, str(e)[:300])
            _annotate("warning", f"Gemini {route}", str(e)[:300])
            continue
        if _gemini_route != route:
            log.info("Gemini отвечает через способ %s", route)
            _gemini_route = route
        return result
    raise LLMError("Gemini не ответил ни одним способом. " + " | ".join(errors))


def _call_anthropic(model: str, system: str, user: str, name: str, schema: dict, max_tokens: int) -> dict:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "tools": [{"name": name, "description": "Верни результат.", "input_schema": schema}],
        "tool_choice": {"type": "tool", "name": name},
        "messages": [{"role": "user", "content": user}],
    }
    headers = {"x-api-key": CFG.llm_api_key, "anthropic-version": "2023-06-01"}
    data = _post_with_retry("https://api.anthropic.com/v1/messages", headers, body, model=model)
    for block in data["content"]:
        if block["type"] == "tool_use":
            return block["input"]
    raise LLMError("Модель не вернула результат")


# max_tokens с запасом: у новых моделей «размышления» тоже считаются в ответ,
# а платишь всё равно только за реально потраченное
def _call(models, system: str, user: str, name: str, schema: dict, max_tokens: int = 3000) -> dict:
    """models — одна модель или список запасных: если у первой кончился дневной лимит, берём следующую."""
    fn = {"anthropic": _call_anthropic, "gemini": _call_gemini}.get(CFG.llm_provider, _call_openai_compat)
    chain = [models] if isinstance(models, str) else [m for i, m in enumerate(models) if m and m not in models[:i]]
    today = quota_day()
    last_quota = None
    for model in chain:
        if EXHAUSTED.get(model) == today:
            continue
        try:
            for attempt in range(2):
                result = fn(model, system, user, name, schema, max_tokens)
                missing = [k for k in schema["required"] if k not in result]
                if not missing:
                    return result
                log.warning("В ответе модели нет полей %s, повторяю", missing)
            raise LLMError(f"Модель дважды вернула неполный ответ: {result}")
        except LLMQuotaError as e:
            last_quota = e
            if "время" in str(e):
                raise
            log.warning("У модели %s кончился бесплатный лимит на сегодня", model)
            _annotate("warning", "Дневной лимит", f"{model}: {e}")
            EXHAUSTED[model] = today
    raise last_quota or LLMQuotaError("у всех моделей кончился дневной лимит")


SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "Интересность 0–10"},
        "category": {"type": "string", "enum": CATEGORIES},
        "title_ru": {"type": "string", "description": "Суть новости по-русски, одна строка"},
        "duplicate_of": {"type": "integer", "description": "id такой же уже опубликованной истории, иначе 0"},
        "reason": {"type": "string", "description": "Почему такая оценка, коротко"},
    },
    "required": ["score", "category", "title_ru", "duplicate_of", "reason"],
}


def score(title: str, summary: str, text: str, source: str, recent: list) -> dict:
    recent_txt = "\n".join(f"id={r['id']}: {r['title_ru']}" for r in recent) or "(пока ничего)"
    user = (
        f"Источник: {source}\nЗаголовок: {title}\nАнонс: {summary}\n\n"
        f"Текст статьи (начало):\n{text[:4000]}\n\n"
        f"Уже опубликовано или в очереди за последние дни:\n{recent_txt}"
    )
    result = _call([CFG.model_fast, CFG.model_writer], SCORE_SYSTEM, user, "score_news", SCORE_SCHEMA, 3000)
    result["score"] = max(0, min(10, int(result["score"])))
    return result


WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "emoji": {"type": "string", "description": "Один эмодзи под тему"},
        "headline": {"type": "string"},
        "body": {"type": "string", "description": "Текст поста, абзацы через пустую строку, без HTML и без markdown"},
        "takeaway": {"type": "string", "description": "Практический вывод одной фразой или пустая строка"},
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "description": "До трёх хэштегов, например #Нячанг #криминал #визы"},
    },
    "required": ["emoji", "headline", "body", "takeaway", "hashtags"],
}


BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "integer", "description": "Интересность 0–10"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "title_ru": {"type": "string", "description": "Суть новости по-русски, одна строка"},
                    "duplicate_of": {"type": "integer", "description": "id такой же истории (из списка уже опубликованных или из этой же пачки), иначе 0"},
                    "reason": {"type": "string", "description": "Почему такая оценка, коротко"},
                },
                "required": ["id", "score", "category", "title_ru", "duplicate_of", "reason"],
            },
        },
    },
    "required": ["items"],
}


def score_batch(items: list, recent: list) -> dict:
    """Оценить сразу пачку новостей одним запросом — так бесплатного лимита хватает на весь день.
    items: [{"id", "source", "title", "summary"}]. Возвращает {id: оценка}."""
    recent_txt = "\n".join(f"id={r['id']}: {r['title_ru']}" for r in recent) or "(пока ничего)"
    lines = [f"id={it['id']} | {it['source']} | {it['title']}" + (f" — {it['summary'][:300]}" if it.get("summary") else "")
             for it in items]
    user = (
        "Оцени каждую новость из списка. Верни оценку для КАЖДОГО id.\n"
        "Если две новости из списка про одно и то же — у менее подробной укажи duplicate_of = id другой.\n\n"
        "НОВОСТИ:\n" + "\n".join(lines) +
        f"\n\nУже опубликовано или в очереди за последние дни:\n{recent_txt}"
    )
    result = _call([CFG.model_fast, CFG.model_writer], SCORE_SYSTEM, user, "score_batch", BATCH_SCHEMA, 8000)
    out = {}
    for r in result.get("items", []):
        try:
            r["score"] = max(0, min(10, int(r.get("score", 0))))
            r["duplicate_of"] = int(r.get("duplicate_of") or 0)
            out[int(r["id"])] = r
        except (TypeError, ValueError):
            continue
    return out


def write_post(title: str, text: str, source: str, url: str, fix_notes: str = "", rates: str = "") -> dict:
    user = f"Источник: {source} ({url})\nЗаголовок: {title}\n\nТекст статьи:\n{text}\n"
    if rates:
        user += f"\nКурс для пересчёта: {rates}\n"
    if fix_notes:
        user += f"\nПрошлый вариант не прошёл проверку фактов. Исправь: {fix_notes}\n"
    result = _call([CFG.model_writer, CFG.model_fast], WRITE_SYSTEM, user, "write_post", WRITE_SCHEMA, 6000)
    result["hashtags"] = result.get("hashtags", [])[:3]
    return result


CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "true, если выдуманных или искажённых фактов нет"},
        "problems": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["ok", "problems"],
}


def fact_check(post_text: str, article_text: str) -> dict:
    user = f"ИСХОДНАЯ СТАТЬЯ:\n{article_text}\n\nРУССКИЙ ПОСТ:\n{post_text}"
    return _call(CFG.model_fast, FACTCHECK_SYSTEM, user, "fact_check", CHECK_SCHEMA, 3000)
