#!/usr/bin/env python3
# loop.py — минимальная локальная петля обучения агентов (пилот П0/П1).
# updated: 2026-06-26
# Только stdlib: sqlite3, json, re, argparse. Без сети, без API, без LLM-вызовов.
# Реализует ядро LEARNING-LOOP-DESIGN.md: record-trace → evaluate → distill → recall.
#
# PII-ДИСЦИПЛИНА: redact() бежит ДО любой записи в БД. В traces попадают только
# *_redacted тексты. Сырой телефон/имя/email/секрет в БД не сохраняется никогда.
#
# Использование:
#   python3 loop.py init [--db ll.sqlite]
#   python3 loop.py record-trace --agent ... --task-kind ... --input ... --output ... \
#                                --trajectory '["tool_a","delete_order"]' [--outcome fail] [--error-class ...]
#   python3 loop.py evaluate --run-id <id>            # или --all для всех неоценённых
#   python3 loop.py distill                            # уроки из fail-трейсов
#   python3 loop.py recall --task-kind ... [--domain-keys ...] [--trust verified,adopted,proposed]

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys
import uuid

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ll.sqlite")
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# --------------------------------------------------------------------------- #
# PII-редактор (шлюз WRITE). Маскирует ДО записи. Возвращает (text, counts).   #
# --------------------------------------------------------------------------- #

# Порядок важен: сначала секреты и email, затем телефоны, затем имена.
_PII_PATTERNS = [
    # Bearer / токены / X-Gateway-Token / пароли в произвольной форме (инвариант C7/F4/F6).
    ("secret", re.compile(
        r"(?i)\b(?:bearer\s+[A-Za-z0-9._\-]{8,}"
        r"|(?:token|secret|password|passwd|pwd|api[_-]?key)\s*[:=]\s*\S+)")),
    # email.
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    # Телефон РФ: +7 / 8 / 7 + 10 цифр с любыми разделителями. Должен идти ДО общих цифр.
    ("phone", re.compile(
        r"(?<!\d)(?:\+?7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}(?!\d)")),
    # Имя: два подряд слова с заглавной (кириллица или латиница) — Имя Фамилия.
    ("name", re.compile(
        r"\b[А-ЯЁA-Z][а-яёa-z]{1,}\s+[А-ЯЁA-Z][а-яёa-z]{1,}\b")),
]


def redact(text):
    """Маскирует PII. Возвращает (redacted_text, {pattern_class: count}).
    Никаких сохранённых значений — только плейсхолдеры и счётчики."""
    if not text:
        return text, {}
    counts = {}
    out = text
    for cls, rx in _PII_PATTERNS:
        n = 0

        def _sub(_m, _cls=cls):
            nonlocal n
            n += 1
            return "<%s_%d>" % (_cls.upper(), n)

        out = rx.sub(_sub, out)
        if n:
            counts[cls] = n
    return out, counts


# --------------------------------------------------------------------------- #
# DB helpers                                                                   #
# --------------------------------------------------------------------------- #

def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn):
    """Идемпотентная миграция новых полей жизненного цикла знания поверх старой схемы.
    До init таблицы нет → table_info пуст → пропускаем."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lessons)").fetchall()]
        if not cols:
            return
        adds = {
            "scope_tag": "TEXT DEFAULT 'global'",            # маршрутизация recall
            "applicability": "TEXT DEFAULT 'Global'",        # области: Global/Architecture/CRM/BI/Bash/Claude Code/Python/Security…
            "half_life": "TEXT DEFAULT 'long'",              # permanent / long / temporary
            "last_verified": "TEXT",                         # дата последней проверки (для пересмотра temporary/long)
        }
        for name, ddl in adds.items():
            if name not in cols:
                conn.execute("ALTER TABLE lessons ADD COLUMN %s %s" % (name, ddl))
        conn.commit()
    except Exception:
        pass


# Области, видимые в ЛЮБОМ проекте (универсальные знания), vs проектные (CRM/BI — только в своём cwd).
UNIVERSAL_SCOPES = {"global", "bash", "tooling", "shell", "security", "python",
                    "architecture", "claude-code", "agent"}
# Окно пересмотра по half-life (дни). permanent — не пересматриваем.
REVIEW_DAYS = {"temporary": 30, "long": 180}


def cmd_init(args):
    with open(SCHEMA, "r", encoding="utf-8") as f:
        ddl = f.read()
    conn = connect(args.db)
    conn.executescript(ddl)
    conn.commit()
    conn.close()
    print("[init] schema applied to %s" % args.db)


# --------------------------------------------------------------------------- #
# (1) record-trace                                                            #
# --------------------------------------------------------------------------- #

def cmd_record_trace(args):
    run_id = args.run_id or ("run-" + uuid.uuid4().hex[:12])
    input_red, c_in = redact(args.input or "")
    output_red, c_out = redact(args.output or "")
    # суммарные счётчики редакции (без значений)
    pii = {}
    for d in (c_in, c_out):
        for k, v in d.items():
            pii[k] = pii.get(k, 0) + v

    trajectory = args.trajectory or "[]"
    try:
        json.loads(trajectory)  # валидируем, что это JSON-массив
    except json.JSONDecodeError:
        print("[record-trace] ERROR: --trajectory must be valid JSON array", file=sys.stderr)
        return 2

    conn = connect(args.db)
    conn.execute(
        """INSERT INTO traces
           (run_id, parent_run_id, agent, task_kind, domain_keys,
            input_redacted, output_redacted, trajectory, outcome, error_class,
            injected_lessons, pii_redactions)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, args.parent_run_id, args.agent, args.task_kind, args.domain_keys,
         input_red, output_red, trajectory, args.outcome, args.error_class,
         args.injected_lessons or "[]", json.dumps(pii, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    print("[record-trace] run_id=%s pii_redacted=%s" % (run_id, json.dumps(pii, ensure_ascii=False)))
    return 0


# --------------------------------------------------------------------------- #
# (2) evaluate — детерминированные правила ($0). Инварианты как тесты.         #
# --------------------------------------------------------------------------- #

# Запрещённые в трейсе действия → (invariant_id, причина). Инварианты из INVARIANTS.md
# компилируются в eval-правила (дизайн §3.2/§5.2).
_FORBIDDEN_TOOLS = {
    "delete_order": ("F1/G", "hard-delete заказа без HARN-маркера — красная зона данных"),
    "update_owner_by_resave": ("A3", "смена владельца пересохранением заказа сбрасывает LineItems"),
    "write_amo": ("G", "запись в Amo до cutover — Amo только чтение"),
    "live_carrier_register": ("C1", "регистрация в боевом кабинете перевозчика вне env=test"),
}


def _evaluate_trace(row):
    """Возвращает (verdict, reason, invariant_id)."""
    traj = []
    try:
        traj = json.loads(row["trajectory"] or "[]")
    except (json.JSONDecodeError, TypeError):
        traj = []

    domain = (row["domain_keys"] or "")

    # Правило 1: явный провал/ошибка в трейсе.
    if row["outcome"] == "fail" or row["error_class"]:
        reason = row["error_class"] or "agent reported outcome=fail"
        return "fail", reason, None

    # Правило 2: инвариант-как-тест — запрещённое действие в траектории.
    for tool in traj:
        if tool in _FORBIDDEN_TOOLS:
            inv, why = _FORBIDDEN_TOOLS[tool]
            # manager_test усиливает критичность, но запрет действует и так.
            scope = " (manager_test!)" if "manager_test" in domain else ""
            return "fail", why + scope, inv

    # Правило 3: иначе — успех.
    return "success", "rules passed: schema ok, no forbidden action", None


def cmd_evaluate(args):
    conn = connect(args.db)
    if args.run_id:
        rows = conn.execute("SELECT * FROM traces WHERE run_id=?", (args.run_id,)).fetchall()
    else:
        # все трейсы без eval
        rows = conn.execute(
            """SELECT t.* FROM traces t
               LEFT JOIN evals e ON e.run_id = t.run_id
               WHERE e.eval_id IS NULL""").fetchall()
    n = 0
    for row in rows:
        verdict, reason, inv = _evaluate_trace(row)
        conn.execute(
            "INSERT INTO evals (run_id, evaluator, verdict, reason, invariant_id) VALUES (?,?,?,?,?)",
            (row["run_id"], "rule", verdict, reason, inv))
        n += 1
        print("[evaluate] %s -> %s (%s)%s"
              % (row["run_id"], verdict, reason, (" [%s]" % inv) if inv else ""))
    conn.commit()
    conn.close()
    if n == 0:
        print("[evaluate] nothing to evaluate")
    return 0


# --------------------------------------------------------------------------- #
# (3) distill — из fail-трейсов формируем короткий урок (semantic).           #
# --------------------------------------------------------------------------- #

def _slug(s):
    s = re.sub(r"[^a-zA-Zа-яёА-ЯЁ0-9]+", "-", (s or "").lower()).strip("-")
    return s[:60]


def cmd_distill(args):
    conn = connect(args.db)
    # fail-трейсы, у которых ещё не извлечён урок (по src_run_id).
    rows = conn.execute(
        """SELECT t.*, e.reason AS fail_reason, e.invariant_id AS inv
           FROM traces t
           JOIN evals e ON e.run_id = t.run_id AND e.verdict = 'fail'
           WHERE t.run_id NOT IN (SELECT src_run_id FROM lessons WHERE src_run_id IS NOT NULL)
           GROUP BY t.run_id""").fetchall()

    made = 0
    for row in rows:
        reason = row["fail_reason"] or row["error_class"] or "unknown failure"
        inv = row["inv"]
        severity = "critical" if inv in ("F1/G", "A3", "C1", "G") else "high"
        # Формат урока: ситуация → что пошло не так → правило на будущее.
        title = "%s: %s" % (row["task_kind"], reason)
        title = title[:120]
        body = (
            "Ситуация: задача '%s' (домен: %s).\n"
            "Что пошло не так: %s.\n"
            "Правило на будущее: перед этим действием прочитать INVARIANTS.md%s "
            "и не выполнять запрещённое действие; при сомнении — стоп и эскалация."
        ) % (row["task_kind"], row["domain_keys"] or "-", reason,
             (" (инвариант %s)" % inv) if inv else "")
        evidence = "trace:%s%s" % (row["run_id"], (" / INVARIANTS.md#%s" % inv) if inv else "")
        dedup_key = "%s|%s" % (row["task_kind"], _slug(reason))

        # dedup/merge: повтор той же ситуации → инкремент support_count.
        existing = conn.execute(
            "SELECT lesson_id, support_count FROM lessons WHERE dedup_key=?", (dedup_key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE lessons SET support_count = support_count + 1, updated_at = datetime('now') "
                "WHERE lesson_id=?", (existing["lesson_id"],))
            print("[distill] merged into lesson_id=%s (support_count+1)" % existing["lesson_id"])
            continue

        cur = conn.execute(
            """INSERT INTO lessons
               (src_run_id, task_kind, domain_keys, scope, title, body, evidence,
                severity, trust, support_count, dedup_key)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (row["run_id"], row["task_kind"], row["domain_keys"], "module",
             title, body, evidence, severity, "proposed", 1, dedup_key))
        made += 1
        print("[distill] new lesson_id=%s trust=proposed severity=%s : %s"
              % (cur.lastrowid, severity, title))
    conn.commit()
    conn.close()
    if made == 0 and not rows:
        print("[distill] no new fail-traces to distill")
    return 0


# --------------------------------------------------------------------------- #
# (4) recall — по task_kind/domain_keys вернуть релевантные уроки.            #
# --------------------------------------------------------------------------- #

def cmd_recall(args):
    trust_levels = [t.strip() for t in (args.trust or "verified,adopted").split(",") if t.strip()]
    placeholders = ",".join("?" for _ in trust_levels)

    # Причинная релевантность: фильтр по task_kind (+ domain_keys), затем trust-фильтр.
    sql = ("SELECT * FROM lessons WHERE task_kind=? AND trust IN (%s)" % placeholders)
    params = [args.task_kind] + trust_levels
    if args.domain_keys:
        sql += " AND (domain_keys IS NULL OR domain_keys LIKE ?)"
        params.append("%" + args.domain_keys + "%")
    sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 " \
           "WHEN 'medium' THEN 2 ELSE 3 END, support_count DESC, updated_at DESC"
    if args.limit:
        sql += " LIMIT %d" % int(args.limit)

    conn = connect(args.db)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    if args.json:
        out = [dict(r) for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("[recall] no lessons for task_kind=%s trust=%s" % (args.task_kind, trust_levels))
        return 0

    print("[recall] %d lesson(s) for task_kind=%s (trust=%s):"
          % (len(rows), args.task_kind, ",".join(trust_levels)))
    for r in rows:
        print("\n--- lesson_id=%s [%s/%s] support=%d ---"
              % (r["lesson_id"], r["trust"], r["severity"], r["support_count"]))
        print(r["body"])
        print("evidence: %s" % r["evidence"])
    return 0


# --------------------------------------------------------------------------- #
# digest — топ-уроки по ВСЕМ task_kind (для SessionStart-инъекции в контекст). #
# --------------------------------------------------------------------------- #

def cmd_digest(args):
    """Топ-N уроков по всем kind — задача на старте сессии ещё неизвестна,
    поэтому подаём самые доверенные/критичные/частые как общий бэкграунд."""
    trust_levels = [t.strip() for t in (args.trust or "verified,adopted").split(",") if t.strip()]
    placeholders = ",".join("?" for _ in trust_levels)
    sql = "SELECT * FROM lessons WHERE trust IN (%s)" % placeholders
    params = list(trust_levels)
    # routing: универсальные области (Global/Bash/Security/…) видны в ЛЮБОМ проекте +
    # проектные (crm/bi) — только в своём cwd.
    scope_tag = getattr(args, "scope_tag", None)
    if scope_tag:
        uni = sorted(UNIVERSAL_SCOPES)
        ph = ",".join("?" for _ in uni)
        sql += " AND (scope_tag IN (%s) OR scope_tag=?)" % ph
        params.extend(uni); params.append(scope_tag)
    sql += (" ORDER BY CASE trust WHEN 'adopted' THEN 0 WHEN 'verified' THEN 1 ELSE 2 END, "
            "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
            "support_count DESC, updated_at DESC LIMIT ?")
    params.append(int(args.limit or 12))
    conn = connect(args.db)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if args.json:
        print(json.dumps([dict(r) for r in rows], ensure_ascii=False))
        return 0
    if not rows:
        return 0  # пусто — нечего инъектировать
    lines = ["УРОКИ ПРОШЛЫХ СЕССИЙ (локальная петля обучения — учитывай перед действием):"]
    for r in rows:
        lines.append("- [%s/%s x%d] %s" % (r["trust"], r["severity"], r["support_count"], r["title"]))
    lines.append("(детали: python3 ~/.claude/learning-loop/loop.py recall --task-kind <kind>)")
    print("\n".join(lines))
    return 0


# --------------------------------------------------------------------------- #
# add-lesson / delete-lesson — запись УТВЕРЖДЁННОГО урока после review-сессии. #
# --------------------------------------------------------------------------- #

def cmd_add_lesson(args):
    """Записать одобренный CEO урок (review → OK). trust=adopted, PII-редакция, dedup, scope_tag."""
    title = (args.title or "").strip()
    body = (args.body or "").strip()
    if not title or not body:
        print("[add-lesson] ERROR: --title и --body обязательны", file=sys.stderr)
        return 2
    title, _ = redact(title)   # защита от PII даже в утверждённом тексте
    body, _ = redact(body)
    scope_tag = (args.scope_tag or "global").strip().lower()
    scope_col = scope_tag if scope_tag in ("agent", "global") else "module"
    task_kind = args.task_kind or scope_tag
    trust = args.trust or "adopted"
    applicability = args.applicability or "Global"
    half_life = (args.half_life or "long").strip().lower()
    today = datetime.date.today().isoformat()
    dedup = "%s|%s" % (scope_tag, _slug(title))
    conn = connect(args.db)
    existing = conn.execute("SELECT lesson_id FROM lessons WHERE dedup_key=?", (dedup,)).fetchone()
    if existing:
        conn.execute("UPDATE lessons SET support_count=support_count+1, updated_at=datetime('now'), "
                     "trust=?, last_verified=? WHERE lesson_id=?", (trust, today, existing["lesson_id"]))
        conn.commit(); conn.close()
        print("[add-lesson] DEDUP → merged into lesson_id=%s (support+1, last_verified=%s)" % (existing["lesson_id"], today))
        return 0
    cur = conn.execute(
        """INSERT INTO lessons (src_run_id, task_kind, domain_keys, scope, scope_tag, title, body,
            evidence, severity, trust, support_count, dedup_key, applicability, half_life, last_verified)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (args.source, task_kind, args.domain_keys, scope_col, scope_tag, title, body,
         args.source or "", args.severity or "medium", trust, 1, dedup, applicability, half_life, today))
    conn.commit(); lid = cur.lastrowid; conn.close()
    print("[add-lesson] lesson_id=%s scope=%s applic=%s half-life=%s trust=%s : %s"
          % (lid, scope_tag, applicability, half_life, trust, title))
    return 0


def cmd_delete_lesson(args):
    conn = connect(args.db)
    cur = conn.execute("DELETE FROM lessons WHERE lesson_id=?", (args.lesson_id,))
    conn.commit(); n = cur.rowcount; conn.close()
    print("[delete-lesson] lesson_id=%s удалён (%d строк)" % (args.lesson_id, n))
    return 0


# --------------------------------------------------------------------------- #
# review-due / verify-lesson — управление жизненным циклом знания.            #
# permanent не стареет; temporary>30д / long>180д — на пересмотр.             #
# --------------------------------------------------------------------------- #

def cmd_review_due(args):
    conn = connect(args.db)
    rows = conn.execute("SELECT lesson_id,title,scope_tag,applicability,half_life,last_verified,trust "
                        "FROM lessons WHERE trust IN ('verified','adopted')").fetchall()
    conn.close()
    today = datetime.date.today()
    due = []
    for r in rows:
        hl = (r["half_life"] or "long").lower()
        if hl == "permanent":
            continue
        win = REVIEW_DAYS.get(hl, 180)
        lv = r["last_verified"]
        try:
            lvd = datetime.date.fromisoformat(lv) if lv else None
        except Exception:
            lvd = None
        age = (today - lvd).days if lvd else 9999
        if age >= win:
            due.append((r, age, win))
    if getattr(args, "json", False):
        print(json.dumps([{**dict(r), "age_days": a, "window": w} for r, a, w in due], ensure_ascii=False))
        return 0
    if not due:
        print("[review-due] нет уроков на пересмотр (temporary>30д / long>180д). permanent не стареет.")
        return 0
    print("[review-due] на пересмотр (%d) — перепроверь актуальность → verify-lesson (актуален) или promote --to deprecated:" % len(due))
    for r, age, win in due:
        print("  - id=%s [%s/%s] last_verified=%s (age %sд > %sд) :: %s"
              % (r["lesson_id"], r["applicability"], r["half_life"], r["last_verified"] or "никогда", age, win, r["title"]))
    return 0


def cmd_verify_lesson(args):
    conn = connect(args.db)
    today = datetime.date.today().isoformat()
    cur = conn.execute("UPDATE lessons SET last_verified=?, updated_at=datetime('now') WHERE lesson_id=?",
                       (today, args.lesson_id))
    conn.commit(); n = cur.rowcount; conn.close()
    print("[verify-lesson] lesson_id=%s last_verified=%s (%d row)" % (args.lesson_id, today, n))
    return 0


# --------------------------------------------------------------------------- #
# promote — перевод урока по trust-гейту (вспомогательно, для П3).            #
# --------------------------------------------------------------------------- #

def cmd_promote(args):
    conn = connect(args.db)
    cur = conn.execute(
        "UPDATE lessons SET trust=?, updated_at=datetime('now') WHERE lesson_id=?",
        (args.to, args.lesson_id))
    conn.commit()
    changed = cur.rowcount
    conn.close()
    print("[promote] lesson_id=%s -> trust=%s (%d row)" % (args.lesson_id, args.to, changed))
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(description="Локальная петля обучения агентов (пилот).")
    p.add_argument("--db", default=DEFAULT_DB, help="путь к SQLite (default: ll.sqlite рядом)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="создать таблицы из schema.sql")

    rt = sub.add_parser("record-trace", help="записать трейс (с PII-редакцией)")
    rt.add_argument("--run-id")
    rt.add_argument("--parent-run-id")
    rt.add_argument("--agent", required=True)
    rt.add_argument("--task-kind", required=True)
    rt.add_argument("--domain-keys")
    rt.add_argument("--input", default="")
    rt.add_argument("--output", default="")
    rt.add_argument("--trajectory", default="[]", help='JSON-массив имён tool, напр. \'["a","delete_order"]\'')
    rt.add_argument("--outcome", default="unknown",
                    choices=["success", "fail", "partial", "blocked", "unknown"])
    rt.add_argument("--error-class")
    rt.add_argument("--injected-lessons", default="[]")

    ev = sub.add_parser("evaluate", help="оценить трейс(ы) правилами")
    ev.add_argument("--run-id", help="конкретный run; без него — все неоценённые")

    sub.add_parser("distill", help="извлечь уроки из fail-трейсов")

    rc = sub.add_parser("recall", help="вернуть релевантные уроки по task_kind/domain")
    rc.add_argument("--task-kind", required=True)
    rc.add_argument("--domain-keys")
    rc.add_argument("--trust", default="verified,adopted",
                    help="уровни доверия через запятую (default verified,adopted)")
    rc.add_argument("--limit", type=int, default=0)
    rc.add_argument("--json", action="store_true")

    pr = sub.add_parser("promote", help="перевести урок по trust-гейту")
    pr.add_argument("--lesson-id", type=int, required=True)
    pr.add_argument("--to", required=True, choices=["proposed", "verified", "adopted", "deprecated"])

    dg = sub.add_parser("digest", help="топ-уроки (для SessionStart-инъекции; по умолч. только approved)")
    dg.add_argument("--trust", default="verified,adopted")
    dg.add_argument("--scope-tag", dest="scope_tag", help="фильтр: global + этот scope (напр. crm)")
    dg.add_argument("--limit", type=int, default=12)
    dg.add_argument("--json", action="store_true")

    al = sub.add_parser("add-lesson", help="записать УТВЕРЖДЁННЫЙ урок (после review)")
    al.add_argument("--title", required=True)
    al.add_argument("--body", required=True)
    al.add_argument("--scope-tag", dest="scope_tag", default="global",
                    help="global / crm / bi / agent / <repo>")
    al.add_argument("--task-kind", dest="task_kind")
    al.add_argument("--domain-keys", dest="domain_keys")
    al.add_argument("--source", help="session-id / run-id / источник")
    al.add_argument("--severity", default="medium", choices=["low", "medium", "high", "critical"])
    al.add_argument("--trust", default="adopted", choices=["proposed", "verified", "adopted"])
    al.add_argument("--applicability", default="Global",
                    help="области через запятую: Global/Architecture/CRM/BI/Bash/Claude Code/Python/Security…")
    al.add_argument("--half-life", dest="half_life", default="long",
                    choices=["permanent", "long", "temporary"])

    dl = sub.add_parser("delete-lesson", help="удалить урок (DELETE OLD)")
    dl.add_argument("--lesson-id", type=int, required=True)

    rd = sub.add_parser("review-due", help="уроки на пересмотр (temporary>30д/long>180д)")
    rd.add_argument("--json", action="store_true")

    vl = sub.add_parser("verify-lesson", help="подтвердить актуальность (last_verified=сегодня)")
    vl.add_argument("--lesson-id", type=int, required=True)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    dispatch = {
        "init": cmd_init,
        "record-trace": cmd_record_trace,
        "evaluate": cmd_evaluate,
        "distill": cmd_distill,
        "recall": cmd_recall,
        "promote": cmd_promote,
        "digest": cmd_digest,
        "add-lesson": cmd_add_lesson,
        "delete-lesson": cmd_delete_lesson,
        "review-due": cmd_review_due,
        "verify-lesson": cmd_verify_lesson,
    }
    return dispatch[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
