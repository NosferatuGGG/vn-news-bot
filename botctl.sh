#!/bin/bash
# Управление ботом из терминала. Запуск без параметров покажет все команды.
DIR="$(cd "$(dirname "$0")" && pwd)"
GH="$(command -v gh || echo "$DIR/.tools/gh")"
REPO="$(git -C "$DIR" remote get-url origin 2>/dev/null | sed -E 's#^https://github.com/##; s#\.git$##')"
B=$'\033[1m'; X=$'\033[0m'

if [ -z "$REPO" ] || [ ! -x "$GH" ]; then
  echo "Бот ещё не установлен. Сначала запусти установщик: bash ~/Downloads/install-vn-news-bot.sh"
  exit 1
fi

case "${1:-help}" in
  run)
    "$GH" workflow run bot.yml -R "$REPO" && echo "Запустил. Журнал через минуту: $0 logs" ;;
  status)
    "$GH" run list -R "$REPO" -w bot.yml -L 8 ;;
  logs)
    ID="$("$GH" run list -R "$REPO" -w bot.yml -L 1 --json databaseId -q '.[0].databaseId')"
    "$GH" run view "$ID" -R "$REPO" --log | grep -E "Запуск бота" | cut -f3- | sed -E 's/^[0-9TZ:.-]+ //' | tail -n 80 ;;
  stop)
    "$GH" workflow disable bot.yml -R "$REPO" && echo "Бот остановлен. Включить: $0 start" ;;
  start)
    "$GH" workflow enable bot.yml -R "$REPO" && echo "Бот снова работает каждые 20 минут." ;;
  auto)
    "$GH" variable set MODE -R "$REPO" --body auto && echo "Теперь посты выходят в канал сами (сомнительные всё равно придут тебе)." ;;
  moderate)
    "$GH" variable set MODE -R "$REPO" --body moderate && echo "Теперь все посты сначала приходят тебе на одобрение." ;;
  set)
    [ $# -eq 3 ] || { echo "Пример: $0 set MAX_POSTS_PER_DAY 6"; exit 1; }
    "$GH" variable set "$2" -R "$REPO" --body "$3" && echo "$2 = $3" ;;
  edit)
    open -e "$DIR/bot/prompts.py" 2>/dev/null || ${EDITOR:-nano} "$DIR/bot/prompts.py"
    echo "Поправь текст, сохрани (Cmd+S) и выполни: $0 save" ;;
  save)
    git -C "$DIR" add -A
    git -C "$DIR" commit -qm "Правки ${2:-}" && git -C "$DIR" push -q origin main \
      && echo "Изменения отправлены. Следующий запуск бота уже их учтёт." \
      || echo "Нечего отправлять — изменений нет." ;;
  open)
    open "https://github.com/$REPO/actions" 2>/dev/null || echo "https://github.com/$REPO/actions" ;;
  *)
    cat <<EOF
${B}Управление ботом «Новости Вьетнама»${X}  (репозиторий: $REPO)

  $0 run        — запустить проверку новостей прямо сейчас
  $0 status     — последние запуски (✓ — успешно, X — ошибка)
  $0 logs       — что бот делал в последний раз
  $0 stop       — остановить бота
  $0 start      — включить снова
  $0 auto       — публиковать без одобрения
  $0 moderate   — присылать черновики на одобрение (по умолчанию)
  $0 set ИМЯ ЗНАЧЕНИЕ — поменять настройку, например: $0 set MAX_POSTS_PER_DAY 6
  $0 edit       — открыть тон и правила канала для правки
  $0 save       — отправить твои правки на GitHub
  $0 open       — открыть страницу запусков в браузере
EOF
    ;;
esac
