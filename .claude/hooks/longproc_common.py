#!/usr/bin/env python3
# Общий модуль для хуков "долгий процесс -> TG". НЕ хук сам по себе.
#
# Модель: продюсеры (longproc-bash-notify.py, longproc-bg-watcher.py,
# longproc-subagent-stop.py) не шлют в Telegram напрямую — они кладут запись в
# сессионную очередь (enqueue). Уведомление реально уходит только из
# longproc-stop-flush.py (хук Stop), и только когда у ЭТОЙ сессии на момент Stop
# нет ни запланированного продолжения (session_crons), ни незакрытых фоновых задач
# (background_tasks) — т.е. когда модель "полностью перестала думать", а не
# закончила один шаг/часть цикла. Так уведомление приходит одно, в конце, а не
# по каждому шагу — это было явно попрошено пользователем 24.08.26 после того, как хук
# в первой версии слал пинг за каждый долгий субагент внутри цикла.
import fcntl
import json
import os
import time

STATE_DIR = os.path.expanduser("~/.claude/hooks/state/longproc")
NOTIFY = os.path.expanduser("~/scripts/notify.sh")
THRESHOLD_SECONDS = 10 * 60  # 24.08.26: было 10 -> 7 -> обратно 10 в тот же день


def queue_path(session_id):
    return os.path.join(STATE_DIR, f"pending_notify-{session_id or 'unknown'}.jsonl")


def enqueue(session_id, text, kind_tg="done"):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = queue_path(session_id)
    item = {"text": text, "kind_tg": kind_tg, "ts": time.time()}
    with open(path, "a") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
