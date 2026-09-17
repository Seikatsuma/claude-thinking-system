#!/usr/bin/env bash
# SessionStart — хвосты прошлой сессии в этом же каталоге не теряются.
# Чат перезапустился (обрыв, /clear, новое окно) — у него новый session_id, и
# реестр проверок начинается с нуля. Без этого хука незакрытые пункты молча
# оставались в старом файле: гейт их больше не видел, пользователь — тоже.
# Показывает только сессии за последние сутки и только из того же каталога,
# чтобы не подтянуть чужой чат, работающий параллельно над другим проектом.
set +e
HOOK_IN="$(cat 2>/dev/null)" python3 - <<'PY' 2>/dev/null
import os, json, sys, time, glob, importlib.util

try:
    d = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    sys.exit(0)
sess, cwd = d.get("session_id") or "", d.get("cwd") or ""
if not sess or not cwd:
    sys.exit(0)

spec = importlib.util.spec_from_file_location(
    "checks", os.path.expanduser("~/scripts/verify/checks.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def topic_of(sid):
    try:
        return json.load(open(os.path.expanduser("~/.claude/hooks/state/topics/sess-%s.json" % sid))).get("topic") or ""
    except Exception:
        return ""
my_topic = topic_of(sess)

# 14.09.26: хвосты ЖИВЫХ чатов не предлагать — чат брался доделывать чужую работу и тратил лимит
live = set()
for _f in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
    try:
        _d = json.load(open(_f)); os.kill(int(_d["pid"]), 0); live.add(_d.get("sessionId"))
    except Exception:
        pass

found = []
for f in glob.glob(os.path.join(m.STATE_DIR, "*.json")):
    if time.time() - os.path.getmtime(f) > 86400:
        continue
    old = os.path.basename(f)[:-5]
    if old == sess or old in live:
        continue
    try:
        st = m.load(old)
    except Exception:
        continue
    cws = st.get("cwds") or ([st["cwd"]] if st.get("cwd") else [])
    if cwd not in cws or not m.is_unfinished(st) or st.get("adopted_by"):
        continue
    # 14.09.26: в /home/claude параллельно живут чаты разных дел — хвост чата сайта всплывал
    # в штабном. Своя тема есть и у хвоста другая — не предлагать; иначе подписать тему и свежесть.
    old_topic = topic_of(old)
    if my_topic and old_topic and old_topic != my_topic:
        continue
    open_items = [c for c in st["checks"] if c.get("verdict") is None]
    found.append((os.path.getmtime(f), old, open_items, len(st.get("touched") or []), old_topic))

if not found:
    sys.exit(0)

found.sort(reverse=True)
lines = ["НЕЗАКРЫТЫЕ ПУНКТЫ ПРОШЛОЙ СЕССИИ в этом каталоге — чат перезапускался, "
         "работа могла оборваться на середине. Забери хвосты в текущую сессию и доведи до вердикта:"]
for mt, old, items, touched, tp in found[:3]:
    age = int((time.time() - mt) / 60)
    note = ("тема «%s»" % tp) if tp else "без темы"
    if age < 30:
        note += "; менялась %d мин назад — возможно, это живой соседний чат: забирай, только если это твоё дело" % age
    lines.append("• сессия %s (%s): пунктов без вердикта %d, правок продукта %d" % (old, note, len(items), touched))
    for c in items[:6]:
        lines.append("    — %s" % c["task"][:90])
    lines.append("  python3 ~/scripts/verify/checks.py --session %s adopt %s" % (sess, old))
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": "\n".join(lines)}},
                 ensure_ascii=False))
sys.exit(0)
PY
exit 0
