#!/usr/bin/env bash
# notify.sh — короткое уведомление пользователю в Telegram.
# Для воркеров, запущенных вставкой промпта в чат (не через worker.sh).
#
# Использование:
#   ~/scripts/notify.sh "текст"                 # ✅ готово
#   ~/scripts/notify.sh "текст" question        # ❓ нужен ответ
#   ~/scripts/notify.sh "текст" error           # ⚠️ проблема
#
# Держать сообщение коротким: одна-две строки. Подробности — в отчёте.

set -uo pipefail

MSG="${1:-}"
KIND="${2:-done}"

[[ -z "$MSG" ]] && { echo "notify.sh: нужен текст сообщения"; exit 1; }

case "$KIND" in
  question) EMOJI="❓" ;;
  error)    EMOJI="⚠️" ;;
  *)        EMOJI="✅" ;;
esac

# Ключ бота и чат — в ~/.secrets/notify.env (chmod 600):
#   NOTIFY_TG_TOKEN=123:ABC
#   NOTIFY_TG_CHAT=123456789
# Нет файла — уведомление молча пропускается, работа не останавливается.
if [[ -f "$HOME/.secrets/notify.env" ]]; then
  set -a; source "$HOME/.secrets/notify.env"; set +a
fi

TOKEN="${NOTIFY_TG_TOKEN:-}"
CHAT="${NOTIFY_TG_CHAT:-}"

if [[ -z "$TOKEN" || -z "$CHAT" ]]; then
  echo "notify.sh: нет ~/.secrets/notify.env (NOTIFY_TG_TOKEN, NOTIFY_TG_CHAT), уведомление пропущено"
  exit 0
fi

RESP=$(curl -sS --max-time 15 "https://api.telegram.org/bot${TOKEN}/sendMessage" \
  -d "chat_id=${CHAT}" \
  --data-urlencode "text=${EMOJI} ${MSG}" 2>&1) || true

if echo "$RESP" | grep -q '"ok":true'; then
  echo "notify.sh: отправлено"
else
  echo "notify.sh: не доставлено — $(echo "$RESP" | head -c 200)"
fi
