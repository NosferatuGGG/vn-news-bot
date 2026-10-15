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
_last_call = 0.0


class LLMError(RuntimeError):
    pass


_annotations = 0


def _annotate(level: str, title: str, text: str):
    """Пометка на странице запуска в GitHub (видна и через API) — чтобы причины сбоев были на виду."""
    global _annotations
    if os.getenv("GITHUB_ACTIONS") != "true" or _annotations >= 8:
        return
    _annotations += 1
    text = text.replace("\n", " ").replace("%", "%25")
    print(f"::{level} title={title}::{text}", flush=True)


def _throttle():
    """Бесплатные тарифы ограничивают число запросов в минуту — держим паузу."""
    global _last_call
    wait = CFG.llm_min_interval_sec - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()


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


def _post_with_retry(url: str, headers: dict, body: dict, attempts: int = 5) -> dict:
    for attempt in range(attempts):
        _throttle()
        r = _http.post(url, headers=headers, json=body)
        if r.status_code in (429, 500, 502, 503, 529):
            delay = float(r.headers.get("retry-after") or 0) or min(60, 5 * 2 ** attempt)
            log.warning("Нейросеть ответила %s, жду %.0f с", r.status_code, delay)
            _annotate("warning", f"Нейросеть {r.status_code}", r.text[:200])
            time.sleep(delay)
            continue
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:500]}")
        return r.json()
    raise LLMError(f"{r.status_code} после {attempts} попыток: {r.text[:400]}")


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
        data = _post_with_retry(url, headers, body)
    except LLMError as e:
        if not str(e).startswith("400"):
            raise
        # модель не умеет схемы — просим просто JSON и даём схему текстом
        body["response_format"] = {"type": "json_object"}
        body["messages"][0]["content"] += "\nСхема: " + json.dumps(schema, ensure_ascii=False)
        data = _post_with_retry(url, headers, body)
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
            data = _post_with_retry(url, headers, body, attempts=3)
            if route == "compat":
                text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            elif route == "generate":
                text = _gemini_generate_text(data)
            else:
                text = _gemini_interactions_text(data)
            result = _extract_json(text)
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
    data = _post_with_retry("https://api.anthropic.com/v1/messages", headers, body)
    for block in data["content"]:
        if block["type"] == "tool_use":
            return block["input"]
    raise LLMError("Модель не вернула результат")


# max_tokens с запасом: у новых моделей «размышления» тоже считаются в ответ,
# а платишь всё равно только за реально потраченное
def _call(model: str, system: str, user: str, name: str, schema: dict, max_tokens: int = 3000) -> dict:
    fn = {"anthropic": _call_anthropic, "gemini": _call_gemini}.get(CFG.llm_provider, _call_openai_compat)
    for attempt in range(2):
        result = fn(model, system, user, name, schema, max_tokens)
        missing = [k for k in schema["required"] if k not in result]
        if not missing:
            return result
        log.warning("В ответе модели нет полей %s, повторяю", missing)
    raise LLMError(f"Модель дважды вернула неполный ответ: {result}")


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
    result = _call(CFG.model_fast, SCORE_SYSTEM, user, "score_news", SCORE_SCHEMA, 3000)
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


def write_post(title: str, text: str, source: str, url: str, fix_notes: str = "", rates: str = "") -> dict:
    user = f"Источник: {source} ({url})\nЗаголовок: {title}\n\nТекст статьи:\n{text}\n"
    if rates:
        user += f"\nКурс для пересчёта: {rates}\n"
    if fix_notes:
        user += f"\nПрошлый вариант не прошёл проверку фактов. Исправь: {fix_notes}\n"
    result = _call(CFG.model_writer, WRITE_SYSTEM, user, "write_post", WRITE_SCHEMA, 6000)
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
