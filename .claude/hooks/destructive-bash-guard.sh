#!/usr/bin/env bash
# PreToolUse(Bash) hook — красная линия «ничего не удалять/сносить без явного «да» пользователя».
# T2: детерминированный deny по паттерну (rm -rf, git reset --hard, git push --force,
# git clean -f, DROP TABLE/TRUNCATE и подобное). НЕ физический барьер (тот же uid владеет
# и хуком, и сессией) — но ловит непреднамеренные/автопилотные разрушительные вызовы и
# заставляет остановиться и спросить, прежде чем действовать.
#
# Bypass после РЕАЛЬНОГО "да" в диалоге: повторить ту же команду с префиксом
#   USER_CONFIRMED=da <команда>
# Это осознанный, видимый в логе вызова шаг — не тихий обход.
#
# Исключение без подтверждения: команда целиком в scratchpad (/tmp/...).
set +e
HOOK_IN="$(cat 2>/dev/null)" python3 - <<'PY' 2>/dev/null
import os, json, re, sys

try:
    d = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    sys.exit(0)

tool = d.get("tool_name") or (d.get("tool") or {}).get("name")
if tool and tool != "Bash":
    sys.exit(0)

ti = d.get("tool_input", {}) or ((d.get("tool") or {}).get("input") or {})
command = ti.get("command") or ""
if not command.strip():
    sys.exit(0)

cwd = str(d.get("cwd") or os.getcwd())

# --- явное подтверждение пользователя (маркер, который Claude добавляет ТОЛЬКО после текста «да») ---
if re.search(r'\bUSER_CONFIRMED=da\b', command):
    sys.exit(0)


def is_tmp_scoped(cmd, cwd):
    abs_paths = re.findall(r'(?<![\w/])/[^\s\'"]+', cmd)
    outside = [p for p in abs_paths if not (p.startswith('/tmp/') or p == '/tmp')]
    if outside:
        return False
    if abs_paths:
        return True
    # нет абсолютных путей вообще (относительные аргументы) — безопасно только если
    # сама сессия/scratchpad уже в /tmp
    return cwd.startswith('/tmp/')


reason = None

# Одноразовые артефакты сборки/окружения — не требуют «да»: их всегда можно пересобрать. Считается safe, только если ВСЕ цели rm -rf в этом
# конкретном вызове — именно такие каталоги (по basename, без wildcard/относительных «..»).
SAFE_BASENAMES = {
    "node_modules", "dist", "build", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".next", ".cache", ".tox", "target", ".eggs",
}


def rm_targets_only_safe(args):
    tokens = [t for t in args.split() if t and not t.startswith('-')]
    if not tokens:
        return False
    for t in tokens:
        if '*' in t or '..' in t:
            return False
        bn = t.rstrip('/').split('/')[-1]
        if bn not in SAFE_BASENAMES:
            return False
    return True


# 1) rm с одновременно recursive + force (в любом порядке, коротких/длинных флагах)
for m in re.finditer(r'(?:^|[;&|]\s*|\bsudo\s+)rm\s+([^\n;&|]*)', command):
    args = m.group(1)
    flag_chars = "".join(re.findall(r'(?<!\S)-([a-zA-Z-]+)', args)).lower()
    has_r = 'r' in flag_chars or '--recursive' in args
    has_f = 'f' in flag_chars or '--force' in args
    if has_r and has_f:
        if rm_targets_only_safe(args):
            continue  # только одноразовые артефакты — пропускаем без «да»
        reason = "rm -rf (рекурсивное безвозвратное удаление)"
        break

# 2) остальные явно деструктивные паттерны
if not reason:
    PATTERNS = [
        (r'\bgit\s+reset\s+--hard\b',
         "git reset --hard (сброс незакоммиченных/чужих изменений)"),
        (r'\bgit\s+push\b[^\n;&|]*(--force\b|--force-with-lease\b|(?<!\S)-f(?!\S))',
         "git push --force (перезапись истории на remote)"),
        (r'\bgit\s+clean\s+[^\n;&|]*-[a-zA-Z]*f',
         "git clean -f (безвозвратное удаление untracked-файлов)"),
        (r'\bgit\s+branch\s+-D\b',
         "git branch -D (принудительное удаление ветки)"),
        (r'\bDROP\s+(TABLE|DATABASE|SCHEMA)\b',
         "DROP TABLE/DATABASE (SQL, безвозвратно)"),
        (r'\bTRUNCATE\s+(TABLE\s+)?\S+',
         "TRUNCATE TABLE (SQL, безвозвратно)"),
        (r'\bmkfs(\.\w+)?\b',
         "mkfs (форматирование раздела)"),
        (r'\bdd\s+[^\n;&|]*\bof=/dev/\S+',
         "dd на /dev/... (перезапись диска)"),
        (r'\bshred\b',
         "shred (безвозвратное уничтожение файла)"),
    ]
    for pat, label in PATTERNS:
        if re.search(pat, command, re.IGNORECASE):
            reason = label
            break

if not reason:
    sys.exit(0)

if is_tmp_scoped(command, cwd):
    sys.exit(0)

msg = (
    "СТОП: команда похожа на деструктивную операцию (%s) вне scratchpad/tmp. "
    "Красная линия CLAUDE.md — ничего не удалять/сносить без явного «да» пользователя текстом в "
    "диалоге. Если подтверждения ещё не было — спроси пользователя и дождись «да». "
    "Если пользователь УЖЕ явно подтвердил в этом диалоге — повтори эту же команду с префиксом "
    "USER_CONFIRMED=da <команда>, тогда хук пропустит." % reason
)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": msg
}}, ensure_ascii=False))
PY
exit 0
