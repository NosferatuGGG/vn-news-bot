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


def test_gemini_json_schema_and_clamp(monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((str(req.url), body))
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(SCORE) + "\n```"}}]})

    _mock(monkeypatch, handler, "gemini")
    res = llm.score("t", "s", "text", "src", [])
    url, body = seen[0]
    assert "generativelanguage.googleapis.com" in url
    assert body["response_format"]["type"] == "json_schema"
    assert body["model"] == CFG.model_fast
    assert res["score"] == 10  # 12 обрезано до 10


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
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(next(answers))}}]})

    _mock(monkeypatch, handler, "gemini")
    assert llm.fact_check("пост", "статья")["problems"] == ["цифра"]
