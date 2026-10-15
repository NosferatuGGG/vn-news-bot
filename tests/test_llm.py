"""Проверка запросов к нейросетям на подменённом сервере (без сети и ключей)."""
import json

import httpx

from bot import llm
from bot.config import CFG

SCORE = {"score": 12, "category": "crime", "title_ru": "Кража у туристов", "duplicate_of": 0, "reason": "Нячанг"}


def _mock(monkeypatch, handler, provider):
    monkeypatch.setattr(CFG, "llm_provider", provider)
    monkeypatch.setattr(CFG, "llm_api_key", "k")
    monkeypatch.setattr(CFG, "llm_min_interval_sec", 0)
    monkeypatch.setattr(llm, "_http", httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)


def _gemini_ok(payload, thought=True):
    """Ответ классического generateContent."""
    parts = [{"text": "думаю…", "thought": True}] if thought else []
    parts.append({"text": "```json\n" + json.dumps(payload) + "\n```"})
    return httpx.Response(200, json={"candidates": [{"content": {"parts": parts}, "finishReason": "STOP"}]})


def _interactions_ok(payload):
    """Ответ Interactions API: шаги ввода, размышлений и ответа модели."""
    return httpx.Response(200, json={"id": "i1", "steps": [
        {"type": "user_input", "content": [{"type": "text", "text": "{не тот json}"}]},
        {"type": "thought", "content": [{"type": "text", "text": "{размышления}"}]},
        {"type": "model_output", "content": [{"type": "text", "text": json.dumps(payload)}]},
    ]})


def test_gemini_interactions_with_new_aq_key(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return _interactions_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    monkeypatch.setattr(CFG, "llm_api_key", "AQ.Ab8-new-style-key")
    res = llm.score("t", "s", "text", "src", [])
    req = seen[0]
    body = json.loads(req.content)
    assert str(req.url).endswith("/v1beta/interactions")
    assert req.headers["x-goog-api-key"] == "AQ.Ab8-new-style-key"
    assert "authorization" not in req.headers  # ключ отправляется ровно один раз
    assert body["model"] == CFG.model_fast and body["store"] is False
    assert body["response_format"]["schema"]["required"][0] == "score"
    assert res["score"] == 10  # 12 обрезано до 10; ввод и размышления не перепутаны с ответом


def test_gemini_falls_back_to_generate_content(monkeypatch):
    calls = []

    def handler(req):
        url = str(req.url)
        body = json.loads(req.content)
        if url.endswith("/interactions"):
            calls.append("interactions")
            return httpx.Response(404, json={"error": {"message": "Not found"}})
        if "responseJsonSchema" in body["generationConfig"]:
            calls.append("generate+schema")
            return httpx.Response(400, json={"error": {"message": "Unknown name responseJsonSchema"}})
        calls.append("generate")
        return _gemini_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    assert llm.score("t", "s", "x", "src", [])["category"] == "crime"
    assert calls == ["interactions", "generate+schema", "generate"]


def test_gemini_bad_key_stops_immediately(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}})

    _mock(monkeypatch, handler, "gemini")
    try:
        llm.score("t", "s", "x", "src", [])
        raise AssertionError("ожидалась ошибка ключа")
    except llm.LLMError as e:
        assert "API key" in str(e)
    assert len(calls) == 1


def test_fallback_to_json_object_on_400_and_retry_on_429(monkeypatch):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["response_format"]["type"])
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "1"}, json={})
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "json_schema unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Вот: " + json.dumps(SCORE)}}]})

    _mock(monkeypatch, handler, "openrouter")
    res = llm.score("t", "s", "text", "src", [])
    assert calls == ["json_schema", "json_schema", "json_object"]
    assert res["category"] == "crime"


def test_anthropic_tool_call(monkeypatch):
    def handler(req):
        body = json.loads(req.content)
        assert req.headers["x-api-key"] == "k"
        assert body["tool_choice"] == {"type": "tool", "name": "fact_check"}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "fact_check",
                                                      "input": {"ok": True, "problems": []}}]})

    _mock(monkeypatch, handler, "anthropic")
    assert llm.fact_check("пост", "статья") == {"ok": True, "problems": []}


def test_incomplete_answer_is_retried(monkeypatch):
    answers = iter([{"ok": True}, {"ok": False, "problems": ["цифра"]}])

    def handler(req):
        return _interactions_ok(next(answers))

    _mock(monkeypatch, handler, "gemini")
    assert llm.fact_check("пост", "статья")["problems"] == ["цифра"]
