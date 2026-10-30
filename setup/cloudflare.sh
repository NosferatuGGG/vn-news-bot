#!/bin/bash
# Установка помощника бота на Cloudflare — одной командой в Терминале Mac:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/NosferatuGGG/vn-news-bot/main/setup/cloudflare.sh)
#
# Что делает: входит в Cloudflare, создаёт хранилище, публикует помощника, связывает его с ботом
# на GitHub. Повторный запуск безопасен — он просто обновит помощника.
set -euo pipefail

REPO="NosferatuGGG/vn-news-bot"
RAW="https://raw.githubusercontent.com/$REPO/main"
NAME="vn-news-bot-helper"
DIR="$HOME/vn-news-bot-helper"
TOOLS="$HOME/.vn-news-bot-tools"
B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; X=$'\033[0m'

step() { echo; echo "${B}▶ $1${X}"; }
ok()   { echo "${G}✓${X} $1"; }
die()  { echo; echo "${R}✗ $1${X}"; exit 1; }

# ---------- 1. Программы: Node (для Cloudflare) и GitHub ----------
step "1/6 Проверяю программы"
export PATH="$TOOLS/node/bin:$HOME/vn-news-bot/.tools:$PATH"
if ! command -v npx >/dev/null 2>&1; then
  echo "Скачиваю Node.js (нужен для публикации на Cloudflare)…"
  ARCH="$(uname -m)"; [ "$ARCH" = "arm64" ] || ARCH="x64"
  VER="v22.20.0"
  mkdir -p "$TOOLS"
  curl -fsSL "https://nodejs.org/dist/$VER/node-$VER-darwin-$ARCH.tar.gz" | tar -xz -C "$TOOLS"
  rm -rf "$TOOLS/node" && mv "$TOOLS/node-$VER-darwin-$ARCH" "$TOOLS/node"
fi
ok "Node.js $(node --version)"
GH="$(command -v gh || true)"
[ -n "$GH" ] || die "Не нашёл программу GitHub (gh). Она ставилась вместе с ботом в ~/vn-news-bot/.tools"
if ! "$GH" auth status >/dev/null 2>&1; then
  echo "Нужно войти в GitHub — откроется браузер."
  "$GH" auth login --web --git-protocol https
fi
ok "GitHub: вход есть"

WR=(npx -y wrangler@4)

# ---------- 2. Вход в Cloudflare ----------
step "2/6 Вход в Cloudflare"
"${WR[@]}" logout >/dev/null 2>&1 || true
unset CLOUDFLARE_API_TOKEN
echo "Ниже появится длинная ссылка ${B}https://dash.cloudflare.com/oauth2/…${X}"
echo "1) Скопируйте её целиком и откройте в Chrome или Safari на этом Mac."
echo "2) Проверьте, что вверху страницы нужный аккаунт Cloudflare, и нажмите Allow."
echo "${Y}Если после Allow страница «localhost» не открывается — выключите VPN и запустите команду заново.${X}"
echo "Чтобы войти по ключу вместо ссылки, нажмите Ctrl+C один раз."
echo
trap 'true' INT                     # Ctrl+C прерывает только вход, а не всю установку
LOGIN_OK=1
"${WR[@]}" login --browser=false || LOGIN_OK=0
trap - INT
if [ "$LOGIN_OK" = 0 ]; then
  echo
  echo "${B}Вход по ключу.${X} Откройте в браузере: ${B}https://dash.cloudflare.com/profile/api-tokens${X}"
  echo "  Create Token → строка «Edit Cloudflare Workers» → Use template →"
  echo "  внизу Continue to summary → Create Token → Copy."
  read -r -s -p "Вставьте ключ (Cmd+V, символы не видны) и нажмите Enter: " CLOUDFLARE_API_TOKEN </dev/tty
  echo
  export CLOUDFLARE_API_TOKEN
  [ -n "$CLOUDFLARE_API_TOKEN" ] || die "Ключ пустой"
fi
WHO="$("${WR[@]}" whoami 2>&1)" || { echo "$WHO"; die "Cloudflare не пустил — проверьте ключ или вход"; }
ok "Вход выполнен: $(echo "$WHO" | grep -Eo '[^ ]+@[^ .]+\.[^ ]+' | head -1 || echo 'аккаунт Cloudflare')"

# ---------- 3. Файлы помощника ----------
step "3/6 Скачиваю помощника"
mkdir -p "$DIR" && cd "$DIR"
curl -fsSL "$RAW/worker/worker.js" -o worker.js
ok "Файл помощника: $DIR/worker.js"

# ---------- 4. Хранилище ----------
step "4/6 Хранилище для постов"
KV_ID="$("${WR[@]}" kv namespace list 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    data = []
print(next((n["id"] for n in data if n.get("title") == "'"$NAME"'"), ""))')"
if [ -z "$KV_ID" ]; then
  OUT="$("${WR[@]}" kv namespace create "$NAME" 2>&1)" || { echo "$OUT"; die "Не получилось создать хранилище"; }
  KV_ID="$(echo "$OUT" | grep -Eo '[0-9a-f]{32}' | head -1)"
fi
[ -n "$KV_ID" ] || die "Не нашёл номер хранилища"
ok "Хранилище: $KV_ID"

cat > wrangler.toml <<EOF
name = "$NAME"
main = "worker.js"
compatibility_date = "2026-10-01"

[[kv_namespaces]]
binding = "BOT"
id = "$KV_ID"

[triggers]
crons = ["*/5 * * * *"]

[vars]
GITHUB_REPO = "$REPO"
WORKFLOW = "bot.yml"
RUN_EVERY_MIN = "20"
EOF

# ---------- 5. Публикация ----------
step "5/6 Публикую помощника"
# вывод показываем сразу: Cloudflare может спросить имя для адреса *.workers.dev — ответьте любым словом
LOG="$DIR/deploy.log"
"${WR[@]}" deploy 2>&1 | tee "$LOG" || {
  echo "${Y}Если ошибка про лимит расписаний (cron) — в аккаунте уже 5 расписаний у других ботов."
  echo "Напишите об этом Claude.${X}"
  die "Cloudflare не принял помощника"
}
URL="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.workers\.dev' "$LOG" | head -1)"
[ -n "$URL" ] || die "Не нашёл адрес помощника в ответе Cloudflare (смотрите выше)"
ok "Помощник работает: $URL"

SECRET="$(openssl rand -hex 24)"
printf %s "$SECRET" | "${WR[@]}" secret put SHARED_SECRET >/dev/null
"$GH" auth token | tr -d '\n' | "${WR[@]}" secret put GITHUB_TOKEN >/dev/null
ok "Ключи помощника сохранены"

# ---------- 6. Связь с ботом на GitHub ----------
step "6/6 Связываю с ботом на GitHub"
"$GH" secret set WORKER_SECRET -R "$REPO" --body "$SECRET"
"$GH" variable set WORKER_URL -R "$REPO" --body "$URL"
"$GH" workflow run bot.yml -R "$REPO"
ok "Бот запущен — через 1–2 минуты он передаст помощнику настройки, и кнопки заработают мгновенно"

curl -fsS "$URL/" >/dev/null && ok "Проверка: помощник отвечает"
echo
echo "${G}${B}Готово.${X} Кнопка «Опубликовать» теперь срабатывает за секунды,"
echo "а бот ищет новости каждые 20 минут."
