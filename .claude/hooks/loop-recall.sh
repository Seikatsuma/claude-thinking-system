#!/usr/bin/env bash
# SessionStart hook — инъекция дайджеста УТВЕРЖДЁННЫХ уроков (trust=verified/adopted) в КОНТЕКСТ модели.
# scope-aware: global видны всегда + уроки текущего проекта (по cwd). Неблокирующий (exit 0).
set +e
LL="$HOME/.claude/learning-loop"
[ -f "$LL/loop.py" ] || exit 0

# cwd из stdin (если есть) → scope_tag проекта.
CWD="$(cat 2>/dev/null | python3 -c 'import sys,json;
try: d=json.load(sys.stdin)
except Exception: d={}
print((d or {}).get("cwd","") or (d or {}).get("projectRoot",""))' 2>/dev/null)"
# Одноразовые `claude --print` бот-вызовы (Казначей, ДР-рассылка, ZoomOps и
# любые вызовы из /tmp) — без дайджеста: их вывод уходит людям как есть,
# служебный контекст сессий им противопоказан (утечка 12.07.26).
case "$CWD" in
  /tmp/*|/tmp|*-bot|*-bot/*|*_bot|*_bot/*|*-ops|*-ops/*|*bot/*) exit 0 ;;
esac

SCOPE="global"

DIGEST="$(python3 "$LL/loop.py" digest --scope-tag "$SCOPE" 2>/dev/null)"
[ -z "$DIGEST" ] && exit 0
DIGEST="$DIGEST" python3 - <<'PY' 2>/dev/null
import os, json
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": os.environ.get("DIGEST", "")}}, ensure_ascii=False))
PY
exit 0
