#!/usr/bin/env python3
"""UserPromptSubmit (функция 8): чат сам говорит, что разговор разросся — пора /compact или новый чат.

Молчит почти всегда.
Заполненность = usage последнего ответа модели (input + cache_read + cache_creation) / окно.
Журнал читается кусками: голова 50 КБ + хвост 200 КБ (весь 40-МБ журнал — 360 мс, нельзя).
Пороги 70% / 85%; повтор не раньше +100K токенов; упало ниже 70% (сжатие) — счётчик сброшен.
Окно 1M, если: в журнале есть modelId с [1m], или usage/preTokens уже больше 200K.
Отметка без [1m] НЕ значит 200K: в a4320e54 она чередуется с [1m] при usage 966K (замер 14.09).
Состояние: ~/.claude/hooks/state/context-fill/<session>.json. Любая ошибка — молча exit 0.
"""
import fnmatch, json, os, sys, time

INFO, URGENT, STEP = 0.70, 0.85, 100_000
SMALL, BIG = 200_000, 1_000_000
HEAD_BYTES, TAIL_BYTES = 50_000, 200_000
STATE_DIR = os.path.expanduser("~/.claude/hooks/state/context-fill")
# Бот-вызовы и /tmp — как loop-recall.sh: их вывод уходит людям как есть.
SKIP_CWD = ("/tmp", "/tmp/*", "*-bot", "*-bot/*", "*_bot", "*_bot/*", "*-ops", "*-ops/*", "*bot/*")
SERVICE_MARKERS = ("<task-notification", "<task-id>", "<system-reminder", "Stop hook feedback",
                   "Stop hook blocking error", "[SYSTEM NOTIFICATION", "[MESSAGE FROM NON-USER",
                   "This session is being continued")


def records(lines):
    for line in lines:
        if '"assistant"' in line or '"compact_boundary"' in line or '"model"' in line:
            try:
                yield json.loads(line)
            except Exception:
                continue


def main():
    d = json.loads(os.environ.get("HOOK_IN") or sys.stdin.read() or "{}")
    if d.get("agent_id") or d.get("agentId"):
        return
    cwd = d.get("cwd") or ""
    if any(fnmatch.fnmatch(cwd, p) for p in SKIP_CWD):
        return
    prompt = (d.get("prompt") or "").strip()
    head = prompt[:500]
    if (not prompt or prompt.startswith("/") or any(mk in head for mk in SERVICE_MARKERS)
            or (prompt.startswith("{") and '"type"' in head)):
        return
    sess, tr = d.get("session_id") or "", d.get("transcript_path") or ""
    if not sess or not tr or not os.path.isfile(tr):
        return

    size = os.path.getsize(tr)
    with open(tr, "rb") as f:
        head_lines = f.read(min(size, HEAD_BYTES)).decode("utf-8", "replace").splitlines()[:-1]
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()
        else:
            f.seek(0)
        tail_lines = f.read().decode("utf-8", "replace").splitlines()
    tail = list(records(tail_lines))

    fill, big = 0, False
    for r in reversed(tail):  # последний ответ модели; сжатие после него — берём postTokens
        if r.get("subtype") == "compact_boundary":
            fill = (r.get("compactMetadata") or {}).get("postTokens") or 1
            break
        if r.get("type") == "assistant" and not r.get("isSidechain"):
            u = (r.get("message") or {}).get("usage") or {}
            t = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                + (u.get("cache_creation_input_tokens") or 0)
            if t > 0:
                fill = t
                break
    if not fill:
        return
    for r in list(records(head_lines)) + tail:
        a = r.get("attachment") or {}
        if (a.get("type") == "model" and "[1m]" in str((a.get("identity") or {}).get("modelId", "")).lower()) \
                or ((r.get("compactMetadata") or {}).get("preTokens") or 0) > SMALL:
            big = True
            break
    window = BIG if (big or fill > SMALL) else SMALL
    pct = fill / window

    path = os.path.join(STATE_DIR, "%s.json" % sess)
    try:
        st = json.load(open(path))
    except Exception:
        st = {}
    last = st.get("last_warned_at") or 0
    if pct < INFO:
        if last:  # было предупреждение, а теперь ниже порога — разговор сжали, начинаем заново
            st.update(last_warned_at=0, reset_ts=time.time())
            save(path, st)
        return
    if last and fill - last < STEP:
        return
    st.update(last_warned_at=fill, last_warned_ts=time.time(), window=window)
    save(path, st)

    n = int(pct * 100)
    if pct >= URGENT:
        line = ("Контекст разговора заполнен на %d%% — скоро произойдёт автосжатие, при котором теряется "
                "часть разговора. Рекомендую /compact или /clear." % n)
    else:
        line = ("Контекст разговора заполнен на %d%%. Если качество ответов ухудшилось — напиши /compact "
                "или начни новый чат." % n)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "РАЗГОВОР РАЗРОССЯ. Первой строкой ответа передай пользователю дословно: «%s»" % line}},
        ensure_ascii=False))


def save(path, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
