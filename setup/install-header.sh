#!/bin/bash
# Установщик бота «Новости Вьетнама».
# Запуск: bash ~/Downloads/install-vn-news-bot.sh
# Что делает: распаковывает бота в папку ~/vn-news-bot, ставит программу GitHub (gh)
# в эту же папку и проводит по шагам настройки. В систему ничего не устанавливает.
set -u
DIR="$HOME/vn-news-bot"
B=$'\033[1m'; R=$'\033[31m'; G=$'\033[32m'; X=$'\033[0m'
fail() { echo "${R}✗ $*${X}"; echo "Скопируй этот текст и пришли в чат Claude."; exit 1; }

echo "${B}Установщик бота «Новости Вьетнама»${X}"

# --- 1. Инструменты разработчика Apple (git, python3, curl) ---
if [ "$(uname)" = "Darwin" ] && ! /usr/bin/xcode-select -p >/dev/null 2>&1; then
  echo
  echo "Нужны «Инструменты командной строки» от Apple (бесплатно, 5–10 минут)."
  echo "Сейчас появится окно — нажми «Установить» и дождись окончания."
  /usr/bin/xcode-select --install >/dev/null 2>&1
  echo
  echo "${B}Когда установка закончится, снова выполни ту же команду:${X}"
  echo "  bash \"$0\""
  exit 0
fi
command -v git >/dev/null || fail "Не найден git."
command -v python3 >/dev/null || fail "Не найден python3."
command -v curl >/dev/null || fail "Не найден curl."

# --- 2. Распаковка бота ---
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -d "$DIR" ]; then
  mv "$DIR" "$DIR.backup-$STAMP" || fail "Не удалось переименовать старую папку $DIR"
  mkdir -p "$DIR"
  # сохраняем связь с GitHub и уже скачанную программу gh
  [ -d "$DIR.backup-$STAMP/.git" ] && cp -R "$DIR.backup-$STAMP/.git" "$DIR/"
  [ -d "$DIR.backup-$STAMP/.tools" ] && cp -R "$DIR.backup-$STAMP/.tools" "$DIR/"
  echo "Старая версия сохранена в $DIR.backup-$STAMP"
else
  mkdir -p "$DIR"
fi
decode() { base64 --decode 2>/dev/null || base64 -D 2>/dev/null || openssl base64 -d; }
awk '/^__PAYLOAD_BELOW__$/{found=1; next} found' "$0" > "$DIR/.payload.b64"
decode < "$DIR/.payload.b64" | tar xzf - -C "$DIR" || fail "Файл установщика повреждён. Скачай его заново."
rm -f "$DIR/.payload.b64"
chmod +x "$DIR/botctl.sh"
echo "${G}✓ Бот распакован в $DIR${X}"

# --- 3. Программа GitHub (gh) ---
GH="$(command -v gh || true)"
if [ -z "$GH" ] && [ -x "$DIR/.tools/gh" ]; then GH="$DIR/.tools/gh"; fi
if [ -z "$GH" ]; then
  echo "Скачиваю программу GitHub (gh)…"
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) PAT='macOS_(arm64|universal)\.zip' ;;
    Darwin-*)     PAT='macOS_(amd64|universal)\.zip' ;;
    Linux-aarch64|Linux-arm64) PAT='linux_arm64\.tar\.gz' ;;
    *)            PAT='linux_amd64\.tar\.gz' ;;
  esac
  URL="$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest \
        | grep -Eo '"browser_download_url": *"[^"]+"' | cut -d'"' -f4 | grep -E "$PAT" | head -n 1)"
  TMP="$(mktemp -d)"
  if [ -n "$URL" ] && curl -fsSL "$URL" -o "$TMP/gh.pkg"; then
    if [[ "$URL" == *.zip ]]; then unzip -q "$TMP/gh.pkg" -d "$TMP"; else tar xzf "$TMP/gh.pkg" -C "$TMP"; fi
    BIN="$(find "$TMP" -type f -path '*/bin/gh' | head -n 1)"
    if [ -n "$BIN" ]; then
      mkdir -p "$DIR/.tools" && cp "$BIN" "$DIR/.tools/gh" && chmod +x "$DIR/.tools/gh"
      xattr -d com.apple.quarantine "$DIR/.tools/gh" 2>/dev/null || true
      GH="$DIR/.tools/gh"
    fi
  fi
  rm -rf "$TMP"
  if [ -z "$GH" ]; then
    BREW="$(command -v brew || ls "$HOME/.homebrew/bin/brew" /opt/homebrew/bin/brew /usr/local/bin/brew 2>/dev/null | head -n 1)"
    [ -n "$BREW" ] && "$BREW" install gh >/dev/null && GH="$(dirname "$BREW")/gh"
  fi
  [ -n "$GH" ] && [ -x "$GH" ] || fail "Не удалось скачать программу GitHub. Проверь интернет и запусти команду ещё раз."
fi
echo "${G}✓ Программа GitHub готова${X}"

# --- 4. Пошаговая настройка ---
GH_BIN="$GH" VNBOT_DIR="$DIR" python3 "$DIR/setup/setup.py" </dev/tty
exit $?

__PAYLOAD_BELOW__
