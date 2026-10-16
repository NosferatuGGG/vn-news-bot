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
    monkeypatch.setattr(llm, "_gemini_route", None)


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


def _compat_ok(payload):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})


def test_gemini_compat_first_with_aq_key(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        return _compat_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    monkeypatch.setattr(CFG, "llm_api_key", "AQ.Ab8-new-style-key")
    res = llm.score("t", "s", "text", "src", [])
    assert str(seen[0].url).endswith("/v1beta/openai/chat/completions")
    assert seen[0].headers["authorization"] == "Bearer AQ.Ab8-new-style-key"
    assert res["score"] == 10  # 12 обрезано до 10


def test_gemini_switches_route_and_remembers_it(monkeypatch):
    calls = []

    def handler(req):
        url = str(req.url)
        if "/openai/" in url:
            calls.append("compat")
            return httpx.Response(401, json={"error": {"message": "ACCESS_TOKEN_TYPE_UNSUPPORTED"}})
        if url.endswith(":generateContent"):
            calls.append("generate")
            assert req.headers["x-goog-api-key"]
            return _gemini_ok(SCORE)
        calls.append("interactions")
        return _interactions_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    assert llm.score("t", "s", "x", "src", [])["category"] == "crime"
    assert llm.score("t", "s", "x", "src", [])["category"] == "crime"
    assert calls == ["compat", "generate", "generate"]  # второй раз сразу рабочим способом


def test_gemini_interactions_as_last_resort(monkeypatch):
    def handler(req):
        url = str(req.url)
        if url.endswith("/interactions"):
            return _interactions_ok(SCORE)
        return httpx.Response(429, json={"error": {"message": "quota exceeded, limit: 0"}})

    _mock(monkeypatch, handler, "gemini")
    assert llm.score("t", "s", "x", "src", [])["score"] == 10


def test_gemini_all_routes_fail_with_readable_reason(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(400, json={"error": {"message": "API key not valid"}}), "gemini")
    try:
        llm.score("t", "s", "x", "src", [])
        raise AssertionError("ожидалась ошибка")
    except llm.LLMError as e:
        msg = str(e)
        assert "compat:" in msg and "generate:" in msg and "interactions:" in msg and "API key" in msg


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
        return _compat_ok(next(answers))

    _mock(monkeypatch, handler, "gemini")
    assert llm.fact_check("пост", "статья")["problems"] == ["цифра"]


def test_daily_quota_switches_to_backup_model_without_retries(monkeypatch):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        model = body.get("model", "")
        calls.append(model)
        if model == CFG.model_writer:
            return httpx.Response(429, json=[{"error": {"code": 429, "message": "You exceeded your current quota",
                                                        "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}}])
        return _compat_ok({"emoji": "🚨", "headline": "З", "body": "Т", "takeaway": "", "hashtags": []})

    _mock(monkeypatch, handler, "gemini")
    monkeypatch.setattr(llm, "EXHAUSTED", {})
    assert llm.write_post("t", "text", "src", "url")["headline"] == "З"
    assert calls == [CFG.model_writer, CFG.model_fast]          # без повторов и без перебора способов
    assert llm.EXHAUSTED[CFG.model_writer] == llm.quota_day()
    calls.clear()
    llm.write_post("t", "text", "src", "url")
    assert calls == [CFG.model_fast]                            # исчерпанную модель больше не трогаем


def test_per_minute_limit_waits_and_retries(monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json=[{"error": {"message": "quota", "details": [
                {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}, {"retryDelay": "12s"}]}}])
        return _compat_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    monkeypatch.setattr(llm, "EXHAUSTED", {})
    assert llm.score("t", "s", "x", "src", [])["score"] == 10 and len(calls) == 2


def test_deadline_stops_waiting(monkeypatch):
    _mock(monkeypatch, lambda req: httpx.Response(503, json={}), "gemini")
    monkeypatch.setattr(llm, "EXHAUSTED", {})
    monkeypatch.setattr(llm, "DEADLINE", llm.time.time() + 1)
    try:
        llm.score("t", "s", "x", "src", [])
        raise AssertionError("ожидалась остановка по времени")
    except llm.LLMQuotaError as e:
        assert "время" in str(e)
    finally:
        monkeypatch.setattr(llm, "DEADLINE", None)


def test_batch_scoring_parses_every_item(monkeypatch):
    payload = {"items": [
        {"id": 5, "score": 11, "category": "crime", "title_ru": "А", "duplicate_of": 0, "reason": "r"},
        {"id": 6, "score": 2, "category": "other", "title_ru": "Б", "duplicate_of": "5", "reason": "r"},
    ]}
    _mock(monkeypatch, lambda req: _compat_ok(payload), "gemini")
    monkeypatch.setattr(llm, "EXHAUSTED", {})
    res = llm.score_batch([{"id": 5, "source": "s", "title": "x"}, {"id": 6, "source": "s", "title": "y"}], [])
    assert res[5]["score"] == 10 and res[6]["duplicate_of"] == 5


def test_overloaded_model_falls_back_and_is_not_marked_exhausted(monkeypatch):
    calls = []

    def handler(req):
        model = json.loads(req.content).get("model", "")
        calls.append(model)
        if model == CFG.model_fast:
            return httpx.Response(503, json={"error": {"code": 503, "message": "This model is currently experiencing high demand."}})
        return _compat_ok(SCORE)

    _mock(monkeypatch, handler, "gemini")
    monkeypatch.setattr(llm, "EXHAUSTED", {})
    assert llm.score("t", "s", "x", "src", [])["score"] == 10
    assert calls == [CFG.model_fast] * 3 + [CFG.model_writer]   # один способ, без перебора, потом запасная модель
    assert CFG.model_fast not in llm.EXHAUSTED
