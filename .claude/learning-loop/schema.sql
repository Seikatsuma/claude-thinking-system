-- schema.sql — SQLite-схема локальной петли обучения (пилот П0/П1)
-- updated: 2026-06-26
-- Реализует минимальное ядро LEARNING-LOOP-DESIGN.md: trace → eval → lesson(memory).
-- ВАЖНО: в БД пишутся ТОЛЬКО редактированные (redacted) тексты. Сырой PII сюда не попадает.
-- Эта БД gitignored (содержит редактированные трейсы боевого контура).

PRAGMA foreign_keys = ON;

-- (1) WRITE: трейс одного запуска агента (episodic, верхний уровень).
-- Все *_redacted поля уже прошли PII-редактор loop.py ДО записи.
CREATE TABLE IF NOT EXISTS traces (
    run_id            TEXT PRIMARY KEY,            -- стабильный id запуска
    parent_run_id     TEXT,                        -- дерево штаб→воркер→субагент
    agent             TEXT NOT NULL,               -- кто: runtime:okk / worker:bug-audit / staff ...
    task_kind         TEXT NOT NULL,               -- класс задачи — ключ retrieval
    domain_keys       TEXT,                        -- модуль/тип лида/перевозчик (БЕЗ PII), CSV
    input_redacted    TEXT,                        -- вход ПОСЛЕ редакции PII
    output_redacted   TEXT,                        -- итог ПОСЛЕ редакции PII
    trajectory        TEXT,                        -- JSON-массив имён tool-вызовов
    outcome           TEXT NOT NULL DEFAULT 'unknown'
                         CHECK (outcome IN ('success','fail','partial','blocked','unknown')),
    error_class       TEXT,                        -- класс ошибки, если упало
    injected_lessons  TEXT,                        -- JSON-массив lesson_id, впрыснутых в этот запуск
    pii_redactions    TEXT,                        -- JSON: {pattern_class: count} — что вырезано (БЕЗ значений)
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_traces_task ON traces(task_kind, domain_keys);

-- (2) EVALUATION: вердикт по трейсу. Пилот — детерминированные Python-правила ($0).
CREATE TABLE IF NOT EXISTS evals (
    eval_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES traces(run_id) ON DELETE CASCADE,
    evaluator   TEXT NOT NULL DEFAULT 'rule',      -- rule / judge-haiku / judge-sonnet
    verdict     TEXT NOT NULL CHECK (verdict IN ('success','fail')),
    reason      TEXT,                              -- человекочитаемая причина
    invariant_id TEXT,                             -- если правило = инвариант из INVARIANTS.md
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_evals_run ON evals(run_id);

-- (3) MANAGE / memory: дистиллированные уроки (semantic).
-- Trust-гейт: proposed → verified → adopted → deprecated (см. дизайн §3.4).
CREATE TABLE IF NOT EXISTS lessons (
    lesson_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    src_run_id     TEXT,                            -- из какого трейса извлечён
    task_kind      TEXT NOT NULL,                   -- ключ recall
    domain_keys    TEXT,                            -- ключ recall (БЕЗ PII)
    scope          TEXT NOT NULL DEFAULT 'module'   -- agent / module / global
                     CHECK (scope IN ('agent','module','global')),
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,                   -- ситуация → что сломалось → правило на будущее
    evidence       TEXT,                            -- run_id / file:line / вывод команды
    severity       TEXT NOT NULL DEFAULT 'medium'
                     CHECK (severity IN ('low','medium','high','critical')),
    trust          TEXT NOT NULL DEFAULT 'proposed'
                     CHECK (trust IN ('proposed','verified','adopted','deprecated')),
    support_count  INTEGER NOT NULL DEFAULT 1,      -- сколько трейсов подтвердили (dedup-инкремент)
    dedup_key      TEXT UNIQUE,                     -- task_kind|normalized_title для merge
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_lessons_recall ON lessons(task_kind, trust);
