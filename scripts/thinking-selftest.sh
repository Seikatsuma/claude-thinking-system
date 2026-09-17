#!/usr/bin/env bash
# thinking-selftest.sh — проверка установленной системы мышления ДЕЛОМ: каждый хук получает
# тестовый вход и должен ответить так, как задумано. Ничего не удаляет и не запускает тяжёлого:
# хукам только передаётся текст команды, сами команды не выполняются.
# Использование: bash ~/scripts/thinking-selftest.sh   → в конце «ИТОГ: N из M»; код 0 = всё прошло.
set -u
PASS=0; TOTAL=0
S='"session_id":"selftest","cwd":"'"$HOME"'","transcript_path":"/nonexistent"'
HEAVY="ff""mpeg -i in.mp4 out.mp3"   # склейка, чтобы сам этот файл не ловился хуком

check() { # имя, команда хука, вход, ожидаемая подстрока ("" = пустой вывод и код 0)
  local name="$1" cmd="$2" input="$3" want="$4" out rc
  TOTAL=$((TOTAL+1))
  out=$(printf '%s' "$input" | timeout 30 bash -c "$cmd" 2>&1); rc=$?
  if { [ -z "$want" ] && [ $rc -eq 0 ]; } || { [ -n "$want" ] && printf '%s' "$out" | grep -qF -- "$want"; }; then
    PASS=$((PASS+1)); echo "ОК    $name"
  else
    echo "СБОЙ  $name (код $rc): $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
  fi
}

H="$HOME/.claude/hooks"
for f in "$H"/*.sh "$HOME"/scripts/*.sh; do [ -x "$f" ] || echo "ВНИМАНИЕ: нет права на запуск: $f"; done

check "разбор задачи подкладывается"      "$H/task-framing.sh" "{$S,\"prompt\":\"опять не изменилось, сделай чтобы бот работал\"}" "ПРОТОКОЛ ИСПОЛНЕНИЯ"
check "словарь слов пользователя"         "$H/task-framing.sh" "{$S,\"prompt\":\"оставь этого бота и удали старый файл\"}" "СЛОВА ПОЛЬЗОВАТЕЛЯ"
check "удаление останавливается"          "$H/destructive-bash-guard.sh" "{$S,\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"rm -rf ~/важная-папка\"}}" "deny"
check "тяжёлая задача без nice"           "$H/heavy-task-nice-guard.sh" "{$S,\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$HEAVY\"}}" "deny"
check "отцепленный запуск запрещён"       "$H/detached-launch-guard.sh" "{$S,\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"npm run build &\"}}" "systemd-run"
check "systemd-run пропускается"          "$H/detached-launch-guard.sh" "{$S,\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"systemd-run --user --unit=job-x --collect true\"}}" ""
check "подъём темы"                       "HOOK_IN=\"\$(cat)\" python3 $H/topic-prompt.py" "{$S,\"prompt\":\"продолжаем про сайт\"}" ""
check "заполненность контекста"           "HOOK_IN=\"\$(cat)\" python3 $H/context-fill-warn.py" "{$S,\"prompt\":\"привет\"}" ""
check "уроки в начале сессии"             "$H/loop-recall.sh" "{$S}" ""
check "хвосты прошлого чата"              "$H/verify-resume.sh" "{$S}" ""
check "agent.md напоминание"              "$H/agentmd-guard.sh" "{$S,\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/tmp/x.py\"}}" ""
check "реестр проверок (хук)"             "$H/verify-touch.sh" "{$S,\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"/tmp/x.py\"}}" ""
check "долгие процессы: bash"             "HOOK_IN=\"\$(cat)\" python3 $H/longproc-bash-notify.py" "{$S,\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"ls\"},\"tool_response\":{}}" ""
check "долгие процессы: помощник старт"   "HOOK_IN=\"\$(cat)\" python3 $H/longproc-subagent-start.py" "{$S,\"agent_id\":\"selftest\"}" ""
check "долгие процессы: помощник стоп"    "HOOK_IN=\"\$(cat)\" python3 $H/longproc-subagent-stop.py" "{$S,\"agent_id\":\"selftest\"}" ""
check "журнал сессий"                     "$H/loop-record.sh" "{$S}" ""
check "оповещение в конце"                "HOOK_IN=\"\$(cat)\" python3 $H/longproc-stop-flush.py" "{$S}" ""
check "реестр проверок"                   "python3 $HOME/scripts/verify/checks.py --session selftest report" "" "чеклист"
check "темы"                              "python3 $HOME/scripts/topics/topic.py --help" "" "usage"
check "петля обучения"                    "python3 $HOME/.claude/learning-loop/loop.py --help" "" "usage"
check "раннер пачек"                      "bash -n $HOME/scripts/runjob.sh" "" ""

for d in "$HOME"/.claude "$HOME"/.claude-webuser-*; do
  [ -d "$d" ] || continue
  TOTAL=$((TOTAL+1))
  if python3 - "$d/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
cmds = " ".join(h.get("command", "") for arr in d.get("hooks", {}).values() for a in arr for h in a.get("hooks", []))
need = ["task-framing.sh", "destructive-bash-guard.sh", "detached-launch-guard.sh", "heavy-task-nice-guard.sh",
        "verify-touch.sh", "loop-recall.sh", "topic-prompt.py", "longproc-stop-flush.py"]
miss = [n for n in need if n not in cmds]
sys.exit(1 if miss else 0) if not miss else (print("нет хуков:", ", ".join(miss)), sys.exit(1))
PY
  then PASS=$((PASS+1)); echo "ОК    хуки подключены в $d/settings.json"
  else echo "СБОЙ  хуки не подключены в $d/settings.json"; fi
  TOTAL=$((TOTAL+1))
  if [ -f "$d/CLAUDE.md" ] && grep -q "Долгие процессы — работа не должна обрываться" "$d/CLAUDE.md"; then
    PASS=$((PASS+1)); echo "ОК    правила на месте в $d/CLAUDE.md"
  else echo "СБОЙ  в $d/CLAUDE.md нет правил системы мышления"; fi
done

echo "ИТОГ: $PASS из $TOTAL"
[ "$PASS" -eq "$TOTAL" ]
