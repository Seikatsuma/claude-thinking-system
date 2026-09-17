#!/usr/bin/env bash
# PreToolUse(Bash) hook — «долгую работу не отцеплять от сессии».
#
# Повод (06.09.26, claudecodeui): выкатка была запущена отцепленно внутри вызова
# Bash. Задача-обёртка завершилась, система прибрала группу процессов — и запуск
# умер, стоя в очереди за замком. Модель считала, что просто ждёт, и двадцать
# минут не происходило НИЧЕГО: ни работы, ни ошибки, ни уведомления. Пользователь в это
# время сидел в веб-интерфейсе и не видел вообще ничего.
#
# Правильный способ — параметр run_in_background у самого инструмента Bash: он
# отслеживается, переживает завершение вызова и присылает уведомление с кодом
# возврата. Отцепленный процесс не даёт ни одной из этих гарантий.
#
# T2 механический deny+retry: человек не нужен, модель повторяет иначе.
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
command = (ti.get("command") or "")
if not command.strip():
    sys.exit(0)

# Уже отслеживаемый фон — ровно то, чего мы и хотим. Не мешаем.
if ti.get("run_in_background") is True:
    sys.exit(0)


def strip_data(text):
    """Убирает содержимое heredoc'ов и кавычек: это ДАННЫЕ, не команда.

    Без этого хук ловил сам себя — упоминание запрещённой формы внутри
    текстового аргумента (описание урока, сообщение коммита) выглядело как
    её применение. Кавычки НЕ снимаются с bash -c: там внутри настоящая
    команда, но её отцепленность видна по внешнему сегменту, который здесь и
    остаётся.
    """
    text = re.sub(r"<<-?'?(\w+)'?.*?^\1", " ", text, flags=re.S | re.M)
    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    return text


code = strip_data(command)

# Позиция команды: начало, либо после разделителя, возможно с присваиваниями
# переменных перед ней. Слово в середине аргумента запуском не считается.
LAUNCH = re.compile(
    r'(?:^|[;|&\n]|(?:&&|\|\|)\s*)\s*(?:\w+=\S*\s+)*(nohup|setsid)\b'
)

reason = None
m = LAUNCH.search(code)
if m:
    reason = m.group(1)
elif re.search(r'(?:^|[;|&\n])\s*disown\b', code):
    reason = "disown"
else:
    # Голый `&` в конце сегмента = отцепленный запуск. `&&`, `2>&1`, `|&` — нет.
    # `… & wait` и `… & tail --pid=` оставляем: это ожидание в переднем плане,
    # процесс не переживает вызов и ничего не теряется.
    stripped = re.sub(r'&&|\|&|\d*>&\d*', '', code)
    if re.search(r'&\s*(?:$|[;\n])', stripped) and not re.search(r'\bwait\b|--pid=', code):
        reason = "фоновый запуск через &"

# 14.09.26: долгая операция в переднем плане обрывается на 10-й минуте (потолок timeout у Bash, дольше не
# выставить). Бэкап на проде шёл дольше — ожидание оборвалось, уведомление выглядело как сбой базы, судьба бэкапа
# неизвестна. Такие команды — только run_in_background. Быстрая проверка — пометка «# быстро» в конце команды.
if not reason and not re.search(r"#\s*быстро\s*$", command):
    LONG = re.compile(r"\b(pg_dump|pg_dumpall|pg_basebackup|pg_restore|mysqldump|mongodump|mongorestore|restic|borg|"
                      r"rsync|duplicity|rclone)\b|\b\S*back_?up\S*\.(?:sh|py)\b", re.I)
    INSPECT = {"grep", "egrep", "rg", "cat", "less", "head", "tail", "ls", "sed", "awk", "find", "which", "type",
               "man", "echo", "printf", "crontab", "stat", "du", "wc", "file", "diff", "journalctl", "systemctl"}
    OPT_VAL = {"-p", "-i", "-o", "-l", "-J", "-F", "-E", "-b", "-c", "-D", "-L", "-R", "-W", "-w"}
    # Текст в кавычках — данные (пояснения, подписи коммитов), кроме того, что уходит на исполнение:
    # полезная нагрузка ssh и bash -c / sh -c. Её разбираем отдельными сегментами.
    nohd = re.sub(r"<<-?'?(\w+)'?.*?^\1", " ", command, flags=re.S | re.M)
    payloads = [m.group(2) for m in re.finditer(r"\b(?:ssh\b[^'\"\n;|&]*|(?:ba)?sh\s+-c\s*)(['\"])(.*?)\1", nohd, flags=re.S)]
    ssh_hosts = re.findall(r"\bssh\b[^'\"\n;|&]*(?=['\"])", nohd)
    raw = "\n".join([strip_data(command)] + payloads)
    for seg in re.split(r"&&|\|\||[;|\n]", raw):
        if not LONG.search(seg) or re.search(r"--(?:help|version)\b|\s-[Vh]\s*$", seg):
            continue
        toks = [t for t in seg.split() if not re.match(r"^\w+=", t)]
        while toks and toks[0] in ("sudo", "nice", "ionice", "timeout", "env", "-n", "-c", "19", "3"):
            toks = toks[1:]
        if toks and toks[0] == "ssh":
            rest, i = toks[1:], 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in OPT_VAL else 1
            toks = rest[i + 1:]
        if toks and os.path.basename(toks[0]) in INSPECT:
            continue
        reason = "долгая операция в переднем плане"
        break
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "СТОП: бэкап, дамп, восстановление или rsync может идти дольше 10 минут, а "
            "команда в переднем плане обрывается на 10-й минуте — дольше таймаут не выставить (14.09 так оборвалось "
            "ожидание бэкапа на проде). Запусти то же самое с run_in_background=true у Bash: фон ждёт до конца и "
            "присылает код возврата. Если на удалённой машине обрыв ssh убьёт работу — запускай там с логом и "
            "проверяй лог. Если работа должна пережить конец ответа (в веб-интерфейсе фон сессии гаснет вместе с ней) — "
            "systemd-run --user --unit=job-<имя> --collect bash -lc '<команда> > ~/logs/job-<имя>.log 2>&1' и проверяй лог. "
            "Точно быстрая проверка — добавь в конец команды «# быстро»."}}, ensure_ascii=False))
        sys.exit(0)

if not reason:
    sys.exit(0)

msg = (
    "СТОП: %s отцепляет процесс от сессии. Так уже терялась выкатка — задача "
    "умерла молча, стоя в очереди, и двадцать минут не происходило ничего. "
    "Запусти то же самое обычной командой, но с параметром run_in_background=true "
    "у инструмента Bash: он переживает завершение вызова, отслеживается и "
    "присылает уведомление с кодом возврата. Если нужно дождаться чужого "
    "процесса — timeout N tail --pid=<PID> -f /dev/null. Если работа должна пережить "
    "конец ответа (в веб-интерфейсе фоновые задачи гаснут вместе с сессией) — "
    "systemd-run --user --unit=job-<имя> --collect bash -lc '<команда> > ~/logs/job-<имя>.log 2>&1', "
    "затем проверка: systemctl --user status job-<имя> и хвост лога."
    % reason
)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": msg
}}, ensure_ascii=False))
PY
exit 0
