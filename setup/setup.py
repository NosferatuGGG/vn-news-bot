#!/usr/bin/env python3
"""Пошаговая настройка бота: Telegram, ключ нейросети, репозиторий GitHub, секреты, первый запуск.

Запускается установщиком install-vn-news-bot.sh. Только стандартная библиотека Python 3.9+
(та, что уже есть на Mac). В сеть ходит через curl, в GitHub — через программу gh.
"""
import getpass
import json
import os
import subprocess
import sys
import time

PROJECT = os.environ.get("VNBOT_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GH = os.environ.get("GH_BIN", "gh")
TOTAL_STEPS = 6

# ---------- оформление ----------
B, G, Y, R, D, X = "\033[1m", "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def say(text=""):
    print(text, flush=True)


def ok(text):
    say(f"{G}✓ {text}{X}")


def warn(text):
    say(f"{Y}! {text}{X}")


def err(text):
    say(f"{R}✗ {text}{X}")


def step(n, title):
    say()
    say(f"{B}━━━ Шаг {n} из {TOTAL_STEPS}. {title} ━━━{X}")


def ask(prompt, default=""):
    hint = f" [{default}]" if default else ""
    answer = input(f"{B}→ {prompt}{hint}: {X}").strip()
    return answer or default


def ask_secret(prompt):
    say(f"{D}  (вставь через Cmd+V — символы не будут видны, это нормально — и нажми Enter){X}")
    return getpass.getpass(f"→ {prompt}: ").strip()


def yes(prompt, default=True):
    d = "да" if default else "нет"
    answer = ask(f"{prompt} (да/нет)", d).lower()
    return answer.startswith(("д", "y"))


def pause(prompt="Нажми Enter, когда сделаешь"):
    input(f"{B}→ {prompt}… {X}")


def open_url(url):
    say(f"  Открываю: {url}")
    for cmd in (["open", url], ["xdg-open", url]):
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            return
        except FileNotFoundError:
            continue


# ---------- сеть ----------
def http(method, url, headers=None, data=None, timeout=40):
    """HTTP-запрос через curl (на Mac он есть всегда и сам доверяет системным сертификатам)."""
    cmd = ["curl", "-sS", "-m", str(timeout), "-X", method, "-w", "\n%{http_code}", url]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    stdin = None
    if data is not None:
        cmd += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        stdin = json.dumps(data)
    r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        return 0, {"error": r.stderr.strip() or "нет связи"}
    body, _, code = r.stdout.rpartition("\n")
    try:
        parsed = json.loads(body) if body.strip() else {}
    except ValueError:
        parsed = {"raw": body[:500]}
    try:
        return int(code), parsed
    except ValueError:
        return 0, parsed


class Bot:
    def __init__(self, token):
        self.token = token

    def call(self, method, **params):
        code, j = http("POST", f"https://api.telegram.org/bot{self.token}/{method}", data=params)
        if not isinstance(j, dict):
            j = {}
        if code == 0:
            j.setdefault("description", j.get("error", "нет связи с Telegram"))
        return j


# ---------- GitHub ----------
def gh(args, input_text=None, check=True, capture=True):
    r = subprocess.run([GH] + args, input=input_text, text=True,
                       capture_output=capture)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {(r.stderr or r.stdout or '').strip()[:500]}")
    return r


def git(args, check=True):
    r = subprocess.run(["git", "-C", PROJECT] + args, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(r.stderr or r.stdout).strip()[:500]}")
    return r


# ======================================================================
# Шаг 1. GitHub
# ======================================================================
def step_github():
    step(1, "Вход в GitHub")
    say("GitHub — сайт, где будет храниться бот и откуда он будет сам запускаться каждые 20 минут.")
    logged = gh(["auth", "status", "-h", "github.com"], check=False).returncode == 0
    if not logged:
        if not yes("У тебя уже есть аккаунт на GitHub?", default=False):
            say()
            say("Сейчас откроется страница регистрации. Что там сделать:")
            say("  1. Введи почту → «Continue».")
            say("  2. Придумай пароль → «Continue».")
            say("  3. Придумай имя пользователя (латиницей, например ivan-vn) → «Continue».")
            say("  4. Пройди проверку «я не робот» и введи код, который придёт на почту.")
            say("  5. Если GitHub задаёт вопросы про команду и интересы — жми «Skip» внизу.")
            open_url("https://github.com/signup")
            pause("Нажми Enter, когда аккаунт создан и ты вошёл в него в браузере")
        say()
        say("Теперь свяжем этот компьютер с твоим аккаунтом GitHub:")
        say(f"  1. Ниже появится код из 8 символов вида {B}ABCD-1234{X} — запомни его.")
        say("  2. Нажми Enter — откроется браузер.")
        say("  3. Вставь код → «Continue» → «Authorize github».")
        say(f"  {D}Если терминал спросит что-то ещё — просто жми Enter.{X}")
        r = gh(["auth", "login", "-h", "github.com", "-p", "https", "-w", "-s", "workflow"],
               check=False, capture=False)
        if r.returncode != 0:
            raise RuntimeError("Не удалось войти в GitHub. Запусти установщик ещё раз.")
    else:
        # для загрузки файла расписания нужно право «workflow»
        head = gh(["api", "-i", "user"], check=False).stdout or ""
        scopes = next((l for l in head.splitlines() if l.lower().startswith("x-oauth-scopes")), "")
        if "workflow" not in scopes:
            say("Нужно добавить одно разрешение: откроется браузер, вставь код и нажми «Authorize».")
            gh(["auth", "refresh", "-h", "github.com", "-s", "workflow"], check=False, capture=False)
    gh(["auth", "setup-git"], check=False)
    user = json.loads(gh(["api", "user"]).stdout)
    ok(f"Вошли в GitHub как {user['login']}")
    return user


# ======================================================================
# Шаг 2. Бот в Telegram
# ======================================================================
def step_bot():
    step(2, "Создаём бота в Telegram")
    say("Бот — это «аккаунт», от имени которого будут выходить посты. Создаётся через официального бота Telegram.")
    say()
    say("Сейчас откроется @BotFather. Что сделать в Telegram:")
    say("  1. Нажми «Start» (или «Запустить»).")
    say("  2. Отправь команду /newbot")
    say("  3. Он спросит имя — напиши любое, например: Новости Вьетнама")
    say("  4. Он спросит username — латиницей и обязательно с bot на конце, например: vietnam_svoi_bot")
    say("     (если занято — придумай другой)")
    say("  5. В ответ придёт сообщение с длинной строкой вида 123456789:AAH4k... — это токен.")
    say("     Нажми на него, он скопируется.")
    open_url("https://t.me/BotFather")
    while True:
        token = ask_secret("Вставь токен бота")
        if ":" not in token:
            err("Это не похоже на токен. Он выглядит так: 123456789:AAH4k... Попробуй ещё раз.")
            continue
        me = Bot(token).call("getMe")
        if me.get("ok"):
            bot = Bot(token)
            bot.username = me["result"]["username"]
            bot.id = me["result"]["id"]
            ok(f"Бот найден: @{bot.username}")
            return bot
        err(f"Telegram не принял токен ({me.get('description', 'неизвестная ошибка')}). Скопируй ещё раз целиком.")


def poll_updates(bot, want, timeout_sec=300, hint_after=90, hint=""):
    """Ждём нужное событие от Telegram (сообщение от тебя или пост в канале)."""
    offset, started, hinted = 0, time.time(), False
    while time.time() - started < timeout_sec:
        res = bot.call("getUpdates", offset=offset, timeout=20,
                       allowed_updates=["message", "channel_post", "my_chat_member"])
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            found = want(upd)
            if found:
                bot.call("getUpdates", offset=offset, timeout=0)  # помечаем прочитанным
                return found
        if not hinted and hint and time.time() - started > hint_after:
            warn(hint)
            hinted = True
        if not res.get("ok"):
            time.sleep(3)
    return None


def step_owner(bot):
    step(3, "Связываем бота с тобой")
    say("Сюда бот будет присылать черновики постов с кнопками «опубликовать / пропустить».")
    say(f"Сейчас откроется твой бот @{bot.username}. Нажми «Start» и отправь ему любое сообщение, например «привет».")
    open_url(f"https://t.me/{bot.username}")
    say(f"{D}  Жду сообщение…{X}")

    def from_owner(upd):
        msg = upd.get("message")
        if msg and msg.get("chat", {}).get("type") == "private":
            return msg["chat"]
        return None

    while True:
        chat = poll_updates(bot, from_owner, hint="Пока ничего не пришло. Открой бота в Telegram, нажми Start и напиши ему «привет».")
        if chat:
            break
        if not yes("Сообщение так и не пришло. Подождать ещё?"):
            raise RuntimeError("Не дождались сообщения боту.")
    name = chat.get("first_name", "")
    bot.call("sendMessage", chat_id=chat["id"],
             text=f"Привет{', ' + name if name else ''}! Я подключён. Сюда буду присылать черновики новостей.")
    ok(f"Готово, бот знает тебя ({name or chat['id']}) и уже написал тебе в Telegram")
    return chat["id"]


def step_channel(bot):
    step(4, "Подключаем канал")
    say("Канал — куда будут выходить посты. Если канала ещё нет, создай его:")
    say("  • На телефоне: Telegram → значок карандаша → «Создать канал».")
    say("  • На Mac: Telegram → значок «Новое сообщение» вверху → «Новый канал».")
    say("  Название любое. Тип — «Публичный» (тогда у канала будет адрес вида @vietnam_svoi) или «Частный».")
    say()
    say(f"Затем сделай бота администратором канала:")
    say("  1. Открой канал → нажми на его название вверху.")
    say("  2. «Изменить» (карандаш) → «Администраторы» → «Добавить администратора».")
    say(f"  3. В поиске введи {B}@{bot.username}{X} → выбери его.")
    say("  4. Проверь, что включено «Публикация сообщений» (и «Удаление сообщений») → «Готово»/«Сохранить».")
    say()
    say("Как только бот станет администратором, установщик это увидит сам.")
    say(f"{D}  Если бот уже был админом раньше — просто отправь в канал любое сообщение, например «тест».{X}")
    say(f"{D}  Жду…{X}")

    def channel_event(upd):
        mcm = upd.get("my_chat_member")
        if mcm and mcm.get("chat", {}).get("type") == "channel":
            if mcm.get("new_chat_member", {}).get("status") == "administrator":
                return {"chat": mcm["chat"]}
        post = upd.get("channel_post")
        if post:
            return {"chat": post["chat"], "message_id": post["message_id"]}
        return None

    while True:
        found = poll_updates(bot, channel_event, timeout_sec=600, hint_after=120,
                             hint=f"Пока ничего. Проверь, что добавил @{bot.username} в администраторы канала. "
                                  "Если он уже там — отправь в канал «тест».")
        if found:
            chat = found["chat"]
            member = bot.call("getChatMember", chat_id=chat["id"], user_id=bot.id).get("result", {})
            if member.get("status") == "administrator" and member.get("can_post_messages", True):
                if found.get("message_id"):
                    bot.call("deleteMessage", chat_id=chat["id"], message_id=found["message_id"])
                title = chat.get("title", "канал")
                ok(f"Канал подключён: «{title}»" + (f" (@{chat['username']})" if chat.get("username") else ""))
                return chat
            warn("Бот видит канал, но не может публиковать. Включи ему «Публикация сообщений» и отправь в канал «тест».")
            continue
        if not yes("Так и не дождались. Подождать ещё?"):
            raise RuntimeError("Не удалось подключить канал.")


# ======================================================================
# Шаг 5. Ключ нейросети
# ======================================================================
def check_gemini(key):
    code, j = http("GET", "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                   headers={"x-goog-api-key": key})
    if code == 200:
        return "ok", ""
    msg = json.dumps(j, ensure_ascii=False)
    if "location is not supported" in msg.lower():
        return "region", msg
    return "bad", msg[:300]


def check_openrouter(key):
    code, j = http("GET", "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    return ("ok", "") if code == 200 else ("bad", json.dumps(j, ensure_ascii=False)[:300])


def check_anthropic(key):
    code, j = http("GET", "https://api.anthropic.com/v1/models",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return ("ok", "") if code == 200 else ("bad", json.dumps(j, ensure_ascii=False)[:300])


PROVIDERS = {
    "1": ("gemini", "GEMINI_API_KEY", check_gemini),
    "2": ("openrouter", "OPENROUTER_API_KEY", check_openrouter),
    "3": ("anthropic", "ANTHROPIC_API_KEY", check_anthropic),
}


def step_ai():
    step(5, "Ключ нейросети")
    say("Нейросеть отбирает новости и переписывает их по-русски. Варианты:")
    say(f"  {B}1{X} — Google Gemini: бесплатно, карта не нужна (рекомендую)")
    say(f"  2 — OpenRouter: платно, оплата картой или криптой")
    say(f"  3 — Claude от Anthropic: платно, нужна зарубежная карта")
    choice = ask("Выбери вариант", "1")
    provider, secret_name, check = PROVIDERS.get(choice, PROVIDERS["1"])

    if provider == "gemini":
        say()
        say("Сейчас откроется Google AI Studio. Что там сделать:")
        say("  1. Войди своим Google-аккаунтом (обычная почта Gmail).")
        say("  2. Если попросит принять условия — поставь галочку и нажми «Continue»/«Accept».")
        say("  3. Нажми «Create API key». Если спросит про проект — выбери «Create API key in new project».")
        say("  4. Появится ключ — он начинается с AQ. (или AIza… у старых ключей). Нажми «Copy».")
        say(f"  {D}Если страница пишет, что сервис недоступен в твоей стране — Google не выдаёт ключи в России.{X}")
        say(f"  {D}Тогда создай ключ, когда будешь во Вьетнаме или Таиланде, или выбери вариант 2.{X}")
        open_url("https://aistudio.google.com/apikey")
    elif provider == "openrouter":
        say("Открою OpenRouter: войди (можно через Google) → «Create API Key» → скопируй ключ sk-or-…")
        say("Пополни баланс в разделе Credits — без денег на балансе бот работать не будет.")
        open_url("https://openrouter.ai/settings/keys")
    else:
        say("Открою консоль Anthropic: войди → «Create Key» → скопируй ключ sk-ant-…")
        say("Пополни баланс в разделе Billing — без денег на балансе бот работать не будет.")
        open_url("https://console.anthropic.com/settings/keys")

    while True:
        key = ask_secret("Вставь ключ")
        if len(key) < 20:
            err("Ключ слишком короткий — похоже, скопировался не целиком.")
            continue
        status, detail = check(key)
        if status == "ok":
            ok("Ключ работает")
            return provider, secret_name, key
        if status == "region":
            warn("Ключ настоящий, но Google не отвечает запросам из твоей страны.")
            warn("Бот будет работать на серверах GitHub в США, а там Gemini доступен — так что всё в порядке.")
            return provider, secret_name, key
        err(f"Ключ не подошёл: {detail}")
        if not yes("Попробовать вставить ещё раз?"):
            raise RuntimeError("Не удалось проверить ключ нейросети.")


# ======================================================================
# Шаг 6. Репозиторий, секреты, первый запуск
# ======================================================================
def step_deploy(user, bot, owner_id, channel, provider, secret_name, key):
    step(6, "Загружаем бота на GitHub и запускаем")
    login = user["login"]
    name = "vn-news-bot"
    repo = f"{login}/{name}"
    exists = gh(["repo", "view", repo, "--json", "name"], check=False).returncode == 0
    if exists and not yes(f"У тебя уже есть репозиторий {repo}. Обновить бота в нём?"):
        name = ask("Тогда придумай другое имя для нового репозитория", "vn-news-bot-2")
        repo = f"{login}/{name}"
        exists = gh(["repo", "view", repo, "--json", "name"], check=False).returncode == 0

    say("Загружаю файлы…")
    if not os.path.isdir(os.path.join(PROJECT, ".git")):
        git(["init", "-q"])
    git(["config", "user.name", login])
    git(["config", "user.email", f"{user['id']}+{login}@users.noreply.github.com"])
    git(["add", "-A"])
    if git(["diff", "--cached", "--quiet"], check=False).returncode != 0:
        git(["commit", "-qm", "Бот новостей Вьетнама"])
    git(["branch", "-M", "main"])
    url = f"https://github.com/{repo}.git"
    if not exists:
        gh(["repo", "create", name, "--public", "--description", "Новости Вьетнама для русских — автопостинг в Telegram"])
    if git(["remote", "get-url", "origin"], check=False).returncode == 0:
        git(["remote", "set-url", "origin", url])
    else:
        git(["remote", "add", "origin", url])
    git(["push", "-q", "-f", "-u", "origin", "main"])
    ok(f"Код загружен: https://github.com/{repo}")

    say("Сохраняю ключи в секретное хранилище GitHub (их никто не увидит)…")
    secrets = {
        secret_name: key,
        "TELEGRAM_BOT_TOKEN": bot.token,
        "TELEGRAM_CHANNEL_ID": str(channel["id"]),
        "TELEGRAM_ADMIN_CHAT_ID": str(owner_id),
    }
    for sname, value in secrets.items():
        gh(["secret", "set", sname, "-R", repo], input_text=value)
    gh(["variable", "set", "LLM_PROVIDER", "-R", repo, "--body", provider])
    if channel.get("username"):
        gh(["variable", "set", "CHANNEL_SIGNATURE", "-R", repo, "--body", "@" + channel["username"]])
    # разрешаем боту сохранять свою память в репозиторий
    gh(["api", "-X", "PUT", f"repos/{repo}/actions/permissions/workflow",
        "-f", "default_workflow_permissions=write"], check=False)
    ok("Ключи сохранены")

    say("Запускаю бота первый раз…")
    started = False
    for _ in range(15):
        if gh(["workflow", "run", "bot.yml", "-R", repo], check=False).returncode == 0:
            started = True
            break
        time.sleep(4)
    if not started:
        warn("GitHub ещё не увидел расписание. Ничего страшного: бот сам запустится в течение 20 минут.")
        bot.call("sendMessage", chat_id=owner_id,
                 text="✅ Всё настроено. В течение 20 минут начну присылать сюда черновики новостей.")
        return repo
    run_id = ""
    for _ in range(15):
        time.sleep(4)
        r = gh(["run", "list", "-R", repo, "-w", "bot.yml", "-L", "1", "--json", "databaseId", "-q", ".[0].databaseId"],
               check=False)
        run_id = (r.stdout or "").strip()
        if run_id:
            break
    bot.call("sendMessage", chat_id=owner_id, text=(
        "✅ Всё настроено. Каждые 20 минут я читаю вьетнамские новости и присылаю сюда черновики.\n"
        "Под каждым кнопки: «В очередь», «Сейчас», «Мимо». Нажатие срабатывает в течение 20 минут.\n"
        "Первые черновики придут минут через 5–10."))
    say()
    say("Первый запуск идёт 3–8 минут: бот читает новости и прогоняет их через нейросеть.")
    say(f"{D}Можно не ждать и закрыть терминал — черновики сами придут тебе в Telegram.{X}")
    if run_id and gh(["run", "watch", run_id, "-R", repo, "--exit-status", "-i", "10"],
                     check=False, capture=False).returncode != 0:
        err("Первый запуск завершился с ошибкой. Последние строки журнала:")
        log = gh(["run", "view", run_id, "-R", repo, "--log-failed"], check=False).stdout or ""
        say("\n".join(log.splitlines()[-25:]))
        say()
        warn("Скопируй эти строки и пришли их в чат Claude — разберёмся.")
    return repo


def main():
    say(f"{B}Настройка бота «Новости Вьетнама»{X}")
    say("Я всё сделаю сам, тебе нужно будет только нажать несколько кнопок в Telegram и браузере.")
    say(f"{D}Займёт 10–15 минут. Прервать можно в любой момент: Ctrl+C. Запускать заново — безопасно.{X}")
    user = step_github()
    bot = step_bot()
    owner_id = step_owner(bot)
    channel = step_channel(bot)
    provider, secret_name, key = step_ai()
    repo = step_deploy(user, bot, owner_id, channel, provider, secret_name, key)

    say()
    say(f"{G}{B}Готово! Бот работает сам, компьютер можно выключать.{X}")
    say()
    say("Что дальше:")
    say("  • Черновики приходят тебе в Telegram — одобряй кнопками.")
    say(f"  • Страница бота: https://github.com/{repo}")
    say(f"  • Управление — команда {B}~/vn-news-bot/botctl.sh{X} (покажет, что умеет).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        warn("Остановлено. Запусти установщик ещё раз, когда будешь готов — он продолжит с начала.")
        sys.exit(1)
    except RuntimeError as e:
        say()
        err(str(e))
        warn("Если не получается — скопируй текст ошибки и пришли в чат Claude.")
        sys.exit(1)
