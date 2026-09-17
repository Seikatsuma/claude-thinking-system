#!/usr/bin/env python3
"""checks.py — реестр задач и вердиктов проверки на сессию.

Зачем: «сделал» — это не результат, а намерение. Результат = вердикт по каждому
пункту задачи с доказательством. Файл состояния читает Stop-хук (evidence-gate),
поэтому незакрытые пункты физически не дают завершить ответ.

Состояние: ~/.claude/hooks/state/verify/<session_id>.json
Команды:
  plan "<что делаю>" --how "<чем докажу>"      добавить пункт (печатает id)
  verdict <id> pass|partial|fail|unchecked --evidence "..." [--issue "..."] [--issue ...]
  status                                        короткая таблица
  report                                        блок для ответа пользователю
  adopt <old_session>                           забрать незакрытые пункты прошлой сессии
                                                (чат перезапустился — хвосты не теряются)
Все команды принимают --session <id> (или берут $CLAUDE_SESSION_ID / last).
"""
import argparse, json, os, sys, time

STATE_DIR = os.path.expanduser("~/.claude/hooks/state/verify")
LAST = os.path.join(STATE_DIR, "last-session")
VERDICTS = {
    "pass": "ВНЕДРЕНО",
    "partial": "ВНЕДРЕНО ЧАСТИЧНО",
    "fail": "НЕ ВНЕДРЕНО",
    "unchecked": "НЕ ПРОВЕРЕНО",
}


def sid(arg=None):
    s = arg or os.environ.get("CLAUDE_SESSION_ID")
    if not s and os.path.exists(LAST):
        s = open(LAST).read().strip()
    return s or "nosession"


def path(s):
    return os.path.join(STATE_DIR, "%s.json" % s)


def load(s):
    try:
        with open(path(s)) as f:
            return json.load(f)
    except Exception:
        return {"session": s, "started": time.time(), "checks": [], "touched": [], "blocks": 0}


def save(s, d):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path(s) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path(s))
    with open(LAST, "w") as f:
        f.write(s)


def cmd_plan(a):
    s = sid(a.session)
    d = load(s)
    d.setdefault("cwd", os.getcwd())
    cws = d.setdefault("cwds", [d["cwd"]])
    if os.getcwd() not in cws:
        cws.append(os.getcwd())
    cid = max([c["id"] for c in d["checks"]] + [0]) + 1
    d["checks"].append({"id": cid, "task": a.task, "how": a.how or "",
                        "verdict": None, "evidence": "", "issues": [], "ts": time.time()})
    save(s, d)
    print("пункт %d заведён: %s | проверю так: %s" % (cid, a.task, a.how or "— не задано —"))


def cmd_verdict(a):
    s = sid(a.session)
    d = load(s)
    for c in d["checks"]:
        if c["id"] == a.id:
            c["verdict"] = a.value
            c["evidence"] = a.evidence or ""
            c["issues"] = list(a.issue or [])
            c["closed_ts"] = time.time()
            save(s, d)
            print("пункт %d: %s | доказательство: %s%s" % (
                a.id, VERDICTS[a.value], c["evidence"] or "—",
                (" | замечания: " + "; ".join(c["issues"])) if c["issues"] else ""))
            return
    sys.exit("нет пункта %d (есть: %s)" % (a.id, [c["id"] for c in d["checks"]] or "ни одного"))


def _open_items(d):
    return [c for c in d["checks"] if c["verdict"] in (None, "unchecked")]


def is_unfinished(d):
    """Сессия бросила работу недоделанной: есть пункт без вердикта или правки без проверок."""
    if d.get("adopted_by"):
        return False
    checks = d.get("checks") or []
    if any(c.get("verdict") is None for c in checks):
        return True
    return bool(d.get("touched")) and not [c for c in checks if c.get("verdict")]


def cmd_adopt(a):
    s = sid(a.session)
    if a.old == s:
        sys.exit("это та же сессия")
    src, dst = load(a.old), load(s)
    # 14.09.26: пункты ходили пингпонгом между двумя чатами (632120ac <-> b6988783).
    if src.get("adopted_by") and src["adopted_by"] != s:
        sys.exit("сессия %s уже передала пункты в %s — забирай оттуда" % (a.old, src["adopted_by"]))
    if any(c.get("adopted_from") == s for c in src["checks"]):
        sys.exit("кольцевой перенос: в %s лежат пункты, взятые из этой сессии" % a.old)
    if not is_unfinished(src):
        print("в сессии %s нет незакрытых пунктов" % a.old)
        return
    moved = []
    for c in src["checks"]:
        if c.get("verdict") is None:
            cid = max([x["id"] for x in dst["checks"]] + [0]) + 1
            dst["checks"].append(dict(c, id=cid, adopted_from=a.old))
            c["verdict"] = "unchecked"
            c["evidence"] = "перенесено в сессию %s, пункт %d" % (s, cid)
            moved.append(cid)
    known = {t["path"] for t in dst["touched"]}
    dst["touched"].extend(t for t in src["touched"] if t["path"] not in known)
    dst.setdefault("cwd", src.get("cwd") or os.getcwd())
    cws = dst.setdefault("cwds", [dst["cwd"]])
    for c in (src.get("cwds") or [src.get("cwd")]):
        if c and c not in cws:
            cws.append(c)
    src["adopted_by"] = s
    save(a.old, src)
    save(s, dst)
    print("перенесено пунктов: %d (новые номера: %s), правок продукта: %d"
          % (len(moved), moved or "—", len(src["touched"])))


def cmd_status(a):
    s = sid(a.session)
    d = load(s)
    if not d["checks"]:
        print("чеклист пуст (правок продукта: %d)" % len(d["touched"]))
        return
    for c in d["checks"]:
        print("%d. [%s] %s%s" % (c["id"], VERDICTS.get(c["verdict"], "ЖДЁТ ПРОВЕРКИ"), c["task"],
                                 (" — " + c["evidence"]) if c["evidence"] else ""))
    print("открытых пунктов: %d" % len(_open_items(d)))


def cmd_report(a):
    s = sid(a.session)
    d = load(s)
    if not d["checks"]:
        print("(чеклист пуст)")
        return
    for c in d["checks"]:
        print("%d. %s — %s" % (c["id"], c["task"], VERDICTS.get(c["verdict"], "ЖДЁТ ПРОВЕРКИ")))
        print("   проверено так: %s" % (c["evidence"] or c["how"] or "— нечем —"))
        for i in c["issues"]:
            print("   замечание: %s" % i)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--session")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("plan"); q.add_argument("task"); q.add_argument("--how"); q.set_defaults(fn=cmd_plan)
    q = sub.add_parser("verdict"); q.add_argument("id", type=int)
    q.add_argument("value", choices=list(VERDICTS)); q.add_argument("--evidence")
    q.add_argument("--issue", action="append"); q.set_defaults(fn=cmd_verdict)
    q = sub.add_parser("status"); q.set_defaults(fn=cmd_status)
    q = sub.add_parser("report"); q.set_defaults(fn=cmd_report)
    q = sub.add_parser("adopt"); q.add_argument("old"); q.set_defaults(fn=cmd_adopt)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
