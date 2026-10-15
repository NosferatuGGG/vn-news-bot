"""Прогон установщика целиком на подменённых Telegram, Google и GitHub."""
import importlib.util
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

SETUP_PATH = os.path.join(os.path.dirname(__file__), "..", "setup", "setup.py")


def load_setup():
    spec = importlib.util.spec_from_file_location("vnsetup", SETUP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKEN = "123456:AAH-test-token"


class FakeWorld:
    """Telegram + Gemini + GitHub в одном."""

    def __init__(self, gemini_region_block=False, bot_is_admin=True, repo_exists=False):
        self.gemini_region_block = gemini_region_block
        self.bot_is_admin = bot_is_admin
        self.repo_exists = repo_exists
        self.sent = []
        self.gh_calls = []
        self.secrets = {}
        self.variables = {}
        self.updates_served = 0

    # ---- сеть ----
    def http(self, method, url, headers=None, data=None, timeout=40):
        if "api.telegram.org" in url:
            if TOKEN not in url:
                return 401, {"ok": False, "description": "Unauthorized"}
            m = url.rsplit("/", 1)[1]
            if m == "getMe":
                return 200, {"ok": True, "result": {"id": 999, "username": "vietnam_svoi_bot"}}
            if m == "getUpdates":
                if data.get("timeout") == 0:
                    return 200, {"ok": True, "result": []}
                self.updates_served += 1
                if self.updates_served == 1:
                    return 200, {"ok": True, "result": [
                        {"update_id": 1, "message": {"chat": {"id": 42, "type": "private", "first_name": "Илья"}}}]}
                return 200, {"ok": True, "result": [
                    {"update_id": 2, "my_chat_member": {"chat": {"id": -1001, "type": "channel", "title": "Вьетнам",
                                                                 "username": "vietnam_svoi"},
                                                        "new_chat_member": {"status": "administrator"}}}]}
            if m == "getChatMember":
                status = "administrator" if self.bot_is_admin else "member"
                return 200, {"ok": True, "result": {"status": status, "can_post_messages": True}}
            if m in ("sendMessage", "deleteMessage"):
                self.sent.append((m, data))
                return 200, {"ok": True, "result": {}}
        if "generativelanguage" in url:
            if self.gemini_region_block:
                return 400, {"error": {"message": "User location is not supported for the API use."}}
            ok = headers.get("x-goog-api-key", "").startswith("AIza")
            return (200, {"models": []}) if ok else (400, {"error": {"message": "API key not valid"}})
        raise AssertionError(f"неожиданный запрос {url}")

    # ---- GitHub CLI ----
    def gh(self, args, input_text=None, check=True, capture=True):
        self.gh_calls.append(args)
        res = SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["auth", "status"]:
            res.returncode = 0
        elif args[:2] == ["api", "-i"]:
            res.stdout = "HTTP/2 200\nX-Oauth-Scopes: repo, workflow\n"
        elif args == ["api", "user"]:
            res.stdout = json.dumps({"login": "ilya", "id": 7})
        elif args[:2] == ["repo", "view"]:
            res.returncode = 0 if self.repo_exists else 1
        elif args[:2] == ["secret", "set"]:
            self.secrets[args[2]] = input_text
        elif args[:2] == ["variable", "set"]:
            self.variables[args[2]] = args[args.index("--body") + 1]
        elif args[:2] == ["run", "list"]:
            res.stdout = "555\n"
        return res


@pytest.fixture
def run_setup(monkeypatch, tmp_path):
    def _run(world, answers, secrets):
        s = load_setup()
        monkeypatch.setattr(s, "PROJECT", str(tmp_path))
        monkeypatch.setattr(s, "http", world.http)
        monkeypatch.setattr(s, "gh", world.gh)
        monkeypatch.setattr(s, "open_url", lambda url: None)
        monkeypatch.setattr(s.time, "sleep", lambda x: None)
        git_calls = []

        def fake_git(args, check=True):
            git_calls.append(args)
            return SimpleNamespace(returncode=1 if args[:2] in (["diff", "--cached"], ["remote", "get-url"]) else 0,
                                   stdout="", stderr="")

        monkeypatch.setattr(s, "git", fake_git)
        answers, secrets = iter(answers), iter(secrets)
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(s.getpass, "getpass", lambda prompt="": next(secrets))
        code = s.main()
        return code, git_calls

    return _run


def test_happy_path(run_setup):
    world = FakeWorld()
    # ответы: выбор нейросети (Enter = 1 Gemini)
    code, git_calls = run_setup(world, answers=[""], secrets=["плохой", TOKEN, "AIza" + "x" * 30])
    assert code == 0
    assert world.secrets == {
        "GEMINI_API_KEY": "AIza" + "x" * 30,
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_CHANNEL_ID": "-1001",
        "TELEGRAM_ADMIN_CHAT_ID": "42",
    }
    assert world.variables == {"LLM_PROVIDER": "gemini", "CHANNEL_SIGNATURE": "@vietnam_svoi"}
    assert ["repo", "create", "vn-news-bot", "--public", "--description",
            "Новости Вьетнама для русских — автопостинг в Telegram"] in world.gh_calls
    assert ["workflow", "run", "bot.yml", "-R", "ilya/vn-news-bot"] in world.gh_calls
    assert ["push", "-q", "-f", "-u", "origin", "main"] in git_calls
    assert ["remote", "add", "origin", "https://github.com/ilya/vn-news-bot.git"] in git_calls
    # бот поздоровался и в конце отчитался
    texts = [d["text"] for m, d in world.sent if m == "sendMessage"]
    assert texts[0].startswith("Привет, Илья") and texts[-1].startswith("✅ Всё настроено")


def test_gemini_region_block_is_accepted(run_setup):
    world = FakeWorld(gemini_region_block=True)
    code, _ = run_setup(world, answers=[""], secrets=[TOKEN, "AIza" + "y" * 30])
    assert code == 0 and world.secrets["GEMINI_API_KEY"].startswith("AIza")


def test_existing_repo_is_updated(run_setup):
    world = FakeWorld(repo_exists=True)
    # ответы: выбор нейросети, «обновить существующий? — да»
    code, git_calls = run_setup(world, answers=["", "да"], secrets=[TOKEN, "AIza" + "z" * 30])
    assert code == 0
    assert not any(c[:2] == ["repo", "create"] for c in world.gh_calls)


def test_http_via_curl_handles_no_network():
    s = load_setup()
    code, body = s.http("GET", "http://127.0.0.1:9/nothing", timeout=2)
    assert code == 0 and "error" in body


def test_install_header_is_valid_bash():
    path = os.path.join(os.path.dirname(__file__), "..", "setup", "install-header.sh")
    assert subprocess.run(["bash", "-n", path]).returncode == 0
