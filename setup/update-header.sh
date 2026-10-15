#!/bin/bash
# Обновление уже установленного бота «Новости Вьетнама».
# Запуск: bash ~/Downloads/update-vn-news-bot.sh
# Заменяет код бота в ~/vn-news-bot на новую версию, отправляет на GitHub и запускает проверку.
# Ключи, канал и настройки не трогает.
set -u
DIR="$HOME/vn-news-bot"
B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; X=$'\033[0m'
fail() { echo "${R}✗ $*${X}"; echo "Скопируй этот текст и пришли в чат Claude."; exit 1; }

echo "${B}Обновление бота «Новости Вьетнама»${X}"
[ -d "$DIR/.git" ] || fail "Бот ещё не установлен. Сначала запусти установщик: bash ~/Downloads/install-vn-news-bot.sh"
GH="$(command -v gh || echo "$DIR/.tools/gh")"
[ -x "$GH" ] || fail "Не найдена программа GitHub (gh). Запусти установщик ещё раз."
REPO="$(git -C "$DIR" remote get-url origin 2>/dev/null | sed -E 's#^https://github.com/##; s#\.git$##')"
[ -n "$REPO" ] || fail "Не найден адрес хранилища на GitHub. Запусти установщик ещё раз."

TMP="$(mktemp -d)"
decode() { base64 --decode 2>/dev/null || base64 -D 2>/dev/null || openssl base64 -d; }
awk '/^__PAYLOAD_BELOW__$/{found=1; next} found' "$0" > "$TMP/payload.b64"
mkdir -p "$TMP/new"
decode < "$TMP/payload.b64" | tar xzf - -C "$TMP/new" || fail "Файл обновления повреждён. Скачай его заново."
cp -R "$TMP/new/." "$DIR/" || fail "Не удалось скопировать файлы в $DIR"
rm -rf "$TMP"
chmod +x "$DIR/botctl.sh"
echo "${G}✓ Файлы обновлены${X}"

git -C "$DIR" add -A
if git -C "$DIR" diff --cached --quiet; then
  echo "${G}✓ У тебя уже последняя версия${X}"
else
  git -C "$DIR" commit -qm "Обновление бота" || fail "Не удалось сохранить изменения"
  git -C "$DIR" push -q origin main || fail "Не удалось отправить обновление на GitHub"
  echo "${G}✓ Обновление отправлено на GitHub${X}"
fi

"$GH" workflow enable bot.yml -R "$REPO" >/dev/null 2>&1   # если бота выключали — включаем
"$GH" workflow run bot.yml -R "$REPO" >/dev/null 2>&1 \
  && echo "${G}✓ Запустил проверку новостей.${X} Через 5–10 минут посмотри: ${B}~/vn-news-bot/botctl.sh logs${X}" \
  || echo "Бот сам запустится в течение 20 минут."
exit 0

__PAYLOAD_BELOW__
