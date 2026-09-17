#!/usr/bin/env bash
# PostToolUse(Edit|Write) hook — дисциплина само-обновления agent.md.
# Если правишь КОД в подсистеме, у которой рядом (вверх по дереву) есть agent.md/AGENTS.md,
# и эта дока теперь СТАРШЕ изменённого файла → напоминаем обновить её (DoD задачи).
# Детерминированно, неблокирующе (exit 0). Срабатывает только когда дока реально устарела.
set +e
HOOK_IN="$(cat 2>/dev/null)" python3 - <<'PY' 2>/dev/null
import os, json, sys

try:
    d = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    sys.exit(0)

# file_path устойчиво к вариантам схемы (tool_input.file_path | tool.input.file_path)
fp = (d.get("tool_input", {}) or {}).get("file_path") \
    or ((d.get("tool", {}) or {}).get("input", {}) or {}).get("file_path") \
    or d.get("file_path")
if not fp or not os.path.isfile(fp):
    sys.exit(0)

# Напоминаем только для исходного кода (не для самих доков/конфигов).
CODE = (".php", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rb", ".java", ".tpl", ".sql")
base = os.path.basename(fp).lower()
if base in ("agent.md", "agents.md", "claude.md") or not fp.lower().endswith(CODE):
    sys.exit(0)
if os.sep + "docs" + os.sep in fp:
    sys.exit(0)

# Ищем ближайший agent.md/AGENTS.md вверх по дереву (до .git или 6 уровней).
d_dir = os.path.dirname(os.path.abspath(fp))
doc = None
for _ in range(6):
    for name in ("agent.md", "AGENTS.md"):
        cand = os.path.join(d_dir, name)
        if os.path.isfile(cand):
            doc = cand
            break
    if doc or os.path.isdir(os.path.join(d_dir, ".git")):
        break
    parent = os.path.dirname(d_dir)
    if parent == d_dir:
        break
    d_dir = parent

if not doc:
    sys.exit(0)

# Напоминаем, только если дока СТАРШЕ изменённого файла (устарела относительно правки).
try:
    if os.path.getmtime(doc) >= os.path.getmtime(fp):
        sys.exit(0)
except OSError:
    sys.exit(0)

msg = ("Дисциплина agent.md: ты изменил %s, рядом есть %s (он теперь старше правки). "
       "DoD задачи = обнови его, если правка меняет структуру/поведение/инварианты/контракты "
       "подсистемы (плотно, без дублирования кода). Если правка косметическая — можно пропустить."
       % (fp, doc))
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": msg}}, ensure_ascii=False))
PY
exit 0
