#!/usr/bin/env python3
# SubagentStop hook — реальная длительность = сейчас - start_epoch из longproc-subagent-start.py.
# НЕ шлёт в TG напрямую — кладёт запись в очередь (longproc_common.enqueue). Реально
# уходит только из longproc-stop-flush.py на Stop, когда у сессии не осталось ни
# запланированного продолжения, ни других фоновых задач — иначе при цикле из многих
# долгих субагентов пользователь получал бы пинг за каждый отдельный шаг ("часть 13 цикла"),
# что он явно попросил убрать 24.08.26.
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from longproc_common import STATE_DIR, THRESHOLD_SECONDS, enqueue


def main():
    try:
        d = json.loads(os.environ.get("HOOK_IN") or "{}")
    except Exception:
        return
    agent_id = d.get("agent_id")
    if not agent_id:
        return

    path = os.path.join(STATE_DIR, f"subagent-{agent_id}.start")
    try:
        with open(path) as f:
            start = json.load(f)
        os.remove(path)
    except Exception:
        return

    elapsed = time.time() - (start.get("start_epoch") or time.time())
    if elapsed < THRESHOLD_SECONDS:
        return

    agent_type = d.get("agent_type") or start.get("agent_type") or "субагент"
    last_msg = (d.get("last_assistant_message") or "").strip().replace("\n", " ")
    if len(last_msg) > 140:
        last_msg = last_msg[:140] + "…"

    minutes = round(elapsed / 60.0, 1)
    text = f"Субагент «{agent_type}» (~{minutes} мин)"
    if last_msg:
        text += f": {last_msg}"

    enqueue(d.get("session_id") or "", text, "done")


if __name__ == "__main__":
    main()
