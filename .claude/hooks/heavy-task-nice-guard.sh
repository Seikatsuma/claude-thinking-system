#!/usr/bin/env bash
# PreToolUse(Bash) hook — красная линия «тяжёлые CPU/RAM-задачи только с nice/ionice».
# Блокирует ffmpeg/whisper*/diariz*, если ПЕРЕД ними (в том же сегменте команды) нет
# одновременно nice и ionice. T2 механический deny+retry: не требует человека — Claude
# сам добавляет префикс и повторяет команду.
#
# Проверяет каждый сегмент команды отдельно (разделители ; && || | и перевод строки),
# чтобы цепочка вида "nice ionice ffmpeg ... && whisper ..." не проскакивала на втором
# шаге без своего nice/ionice.
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

# whisper без \w*: жадный \w* сжирал "whisperkey" (имя папки) целиком как
# "тяжёлую задачу" — /whisperkey/... матчился, т.к. \w* дотягивал совпадение до реального
# конца слова. diariz\w* оставлен как есть — конфликтующих слов с этим префиксом нет.
HEAVY = re.compile(r'\b(ffmpeg|faster-whisper|whisperx|whisper|diariz\w*)\b', re.IGNORECASE)
SEP = re.compile(r'&&|\|\||[;|\n]')


def segment_bad(seg):
    # чистая проверка наличия бинаря / версии — не грузит CPU, не требует nice/ionice
    if re.match(r'^\s*(which|type|command\s+-v)\s+', seg, re.IGNORECASE):
        return None
    m = HEAVY.search(seg)
    if not m:
        return None
    tail = seg[m.end():m.end() + 40]
    if re.match(r'\s*(-{1,2}version\b|-h\b|--help\b)', tail):
        return None
    prefix = seg[:m.start()]
    if re.search(r'\bnice\b', prefix) and re.search(r'\bionice\b', prefix):
        return None
    return m.group(0)


bad = None
for seg in SEP.split(command):
    bad = segment_bad(seg)
    if bad:
        break

if not bad:
    sys.exit(0)

msg = (
    "СТОП: тяжёлая CPU/RAM-задача (%s) без nice/ionice перед ней в команде. "
    "Красная линия CLAUDE.md — такие задачи на проде запускать только с "
    "`nice -n 19 ionice -c 3` перед командой (иначе сервер лагает и в него не зайти по "
    "SSH чтобы починить). Повтори с префиксом: nice -n 19 ionice -c 3 <твоя команда>."
    % bad
)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": msg
}}, ensure_ascii=False))
PY
exit 0
