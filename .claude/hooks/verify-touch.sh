#!/usr/bin/env bash
# PostToolUse(Edit|Write|Bash) — отмечает, что в сессии менялся ПРОДУКТ.
# Нужен гейту доказательства: правил продукт — покажи результат на конечном экране.
# Bash считаем тоже: в авто-режиме почти все правки идут командой, а не редактором.
# Заметки, брифы, память, отчёты и временные файлы продуктом не считаются.
# Тела heredoc разбираются отдельно: `if x > 0` внутри скрипта — не запись в файл.
# Заодно копит все каталоги, где работала сессия: cwd чата по ходу работы меняется,
# а перенос хвостов после перезапуска (verify-resume.sh) ищет сессию по каталогу.
set +e
HOOK_IN="$(cat 2>/dev/null)" python3 - <<'PY' 2>/dev/null
import os, json, sys, re, time, importlib.util

SKIP = ("/vault/", "/memory/", "/reports/", "/scratchpad", "/tmp/", "/context-ops/",
        "/.claude/hooks/state/", "/logs/", "/node_modules/", "/.git/", "/render/",
        "/dev/", "/proc/")
P = r"[\w./@+~-]"


def strip_heredocs(cmd):
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        out.append(lines[i])
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", lines[i])
        i += 1
        if m:
            while i < len(lines) and lines[i].strip() != m.group(1):
                i += 1
            i += 1
    return "\n".join(out)


try:
    d = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    sys.exit(0)
sess = d.get("session_id") or ""
if not sess:
    sys.exit(0)
cwd = d.get("cwd") or os.getcwd()
ti = d.get("tool_input", {}) or {}
paths, marks = [], []

fp = ti.get("file_path") or d.get("file_path")
if fp:
    paths.append(fp)
else:
    cmd = ti.get("command") or ""
    shell = strip_heredocs(cmd)
    # Запись в файл самой командой (тела heredoc вырезаны).
    for pat in (r"(?:^|[^<>&\w])\d?>>?\s*(%s+)" % P,
                r"\bsed\s+-i\S*\s+(?:-e\s+)?\S+\s+(%s+)" % P,
                r"\btee\s+(?:-a\s+)?(%s+)" % P,
                r"\b(?:cp|mv|install)\s+(?:-\S+\s+)*\S+\s+(%s+)\s*(?:$|[;&|)\n])" % P,
                r"\brsync\s+(?:-\S+\s+)*\S+\s+(%s+)\s*(?:$|[;&|)\n])" % P,
                r"\b(?:curl|wget)\b[^\n;|&]*?\s-[oO]\s*(%s+)" % P,
                r"\btouch\s+(?:-\S+\s+)*(%s+)" % P,
                r"\bln\s+-\S+\s+\S+\s+(%s+)" % P):
        paths.extend(re.findall(pat, shell, re.M))
    # Запись изнутри скрипта — по всей команде, включая тела heredoc и -e/-c.
    for pat in (r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wax]",
                r"write(?:File|FileSync)\(\s*['\"]([^'\"]+)['\"]",
                r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)\.write_(?:text|bytes)"):
        paths.extend(re.findall(pat, cmd))
    # Правки без явного пути: меняют то, что видит человек, — гейт обязан проснуться.
    # Засчитывается только ЗАПУСК. `cat deploy.sh`, `grep "npm run build"` — это чтение:
    # 12.09.26 живой тест прочитал скрипт выкатки, а в реестр записалась «выкатка».
    READERS = {"cat", "less", "more", "head", "tail", "grep", "rg", "egrep", "wc", "stat",
               "ls", "file", "diff", "vim", "vi", "nano", "bat", "awk", "git", "find",
               "readlink", "realpath", "echo", "printf", "shellcheck", "type", "which", "sed"}

    def launched(pat):
        for seg in re.split(r"&&|\|\||[;|\n(){}]", shell):
            m = re.search(pat, seg)
            if not m:
                continue
            lead = seg[:m.start()].split()
            if lead and lead[0] in READERS:
                continue
            if lead and lead[0] in ("bash", "sh", "zsh") and "-n" in lead[1:2]:
                continue
            return m.group(0)
        return None

    for pat, label in ((r"\bgit\s+(?:checkout|restore|apply|revert|reset\s+--hard|stash\s+pop)", "правка через git"),
                       (r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add|remove|uninstall)\b", "зависимости"),
                       (r"\bdocker\s+cp\b", "выкатка"),
                       (r"\b(?:npm|pnpm|yarn|bun)\s+run\s+\S*(?:build|deploy|publish)", "выкатка"),
                       (r"\b(?:vite|webpack|next|tsc)\s+build", "выкатка"),
                       (r"\bmake\b(?!\s*-n)", "выкатка"),
                       (r"systemctl\s+(?:--user\s+)?(?:restart|reload)\s+\S+", "выкатка"),
                       (r"docker\s+compose\s+up", "выкатка"),
                       (r"\S*(?:deploy|build)\S*\.sh\b", "выкатка")):
        hit = launched(pat)
        if hit:
            marks.append("%s: %s" % (label, hit[:60]))


def norm(p):
    p = os.path.expanduser(p)
    if not p.startswith("/"):
        # «0», «b», «&1» — не файлы; относительный путь признаём по расширению или слэшу
        if not re.search(r"/|\.[A-Za-z]\w{0,6}$", p):
            return None
        p = os.path.normpath(os.path.join(cwd, p))
    if any(s in p + "/" for s in SKIP) or p.endswith((".bak", ".tmp", ".log")):
        return None
    return p


paths = [q for q in (norm(p) for p in paths) if q] + marks

try:
    spec = importlib.util.spec_from_file_location(
        "checks", os.path.expanduser("~/scripts/verify/checks.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    exists = os.path.exists(m.path(sess))
    if not paths and not exists:
        sys.exit(0)  # чтение в сессии без правок — файл состояния не заводим
    st = m.load(sess)
    changed = False
    cws = st.setdefault("cwds", [st["cwd"]] if st.get("cwd") else [])
    if cwd and cwd not in cws:
        cws.append(cwd)
        changed = True
    st.setdefault("cwd", cwd)
    known = {t["path"] for t in st["touched"]}
    for p in paths:
        if p not in known:
            st["touched"].append({"path": p, "ts": time.time()})
            known.add(p)
            changed = True
    if changed:
        m.save(sess, st)
except Exception:
    pass
sys.exit(0)
PY
exit 0
