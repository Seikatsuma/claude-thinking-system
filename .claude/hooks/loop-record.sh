#!/usr/bin/env bash
# Stop hook — фиксирует метаданные завершённой сессии в журнал петли (материал для
# периодической дистилляции: отдельный агент позже читает транскрипты → fail-трейсы → distill).
# НЕблокирующий (exit 0). Защита от зацикливания: stop_hook_active → выходим.
set +e
LL="$HOME/.claude/learning-loop"
mkdir -p "$LL" 2>/dev/null
HOOK_IN="$(cat 2>/dev/null)" python3 - "$LL/sessions.jsonl" <<'PY' 2>/dev/null
import sys, os, json, datetime
try:
    data = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    data = {}
if data.get("stop_hook_active"):
    sys.exit(0)  # уже в Stop-хуке — не зацикливаемся
rec = {
    "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    "session_id": data.get("session_id") or data.get("sessionId"),
    "cwd": data.get("cwd") or data.get("projectRoot"),
    "transcript": data.get("transcript_path") or data.get("transcriptPath"),
}
with open(sys.argv[1], "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY
exit 0
