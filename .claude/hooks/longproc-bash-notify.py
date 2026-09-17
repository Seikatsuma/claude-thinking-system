#!/usr/bin/env python3
# PostToolUse(Bash) hook — фиксирует долгую Bash-команду в очередь на TG-уведомление
# (реально шлёт longproc-stop-flush.py на Stop, когда сессия полностью освободилась —
# см. longproc_common.py). Foreground: duration_ms уже известна хуку напрямую.
# Background (run_in_background): длительность здесь ещё не известна — запускаем
# отсоединённый вотчер (longproc-bg-watcher.py), который сам дождётся сентинела
# "[exited with code N]" в output-файле задачи и поставит запись в очередь по факту.
import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from longproc_common import STATE_DIR, THRESHOLD_SECONDS, enqueue

WATCHER = os.path.expanduser("~/.claude/hooks/longproc-bg-watcher.py")


def guess_output_file(session_id, task_id):
    pattern = f"/tmp/claude-{os.getuid()}/*/{session_id}/tasks/{task_id}.output"
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def main():
    try:
        d = json.loads(os.environ.get("HOOK_IN") or "{}")
    except Exception:
        return
    if d.get("tool_name") != "Bash":
        return

    ti = d.get("tool_input") or {}
    tr = d.get("tool_response") or {}
    desc = (ti.get("description") or ti.get("command") or "команда").strip()[:120]
    session_id = d.get("session_id") or ""

    bg_task_id = tr.get("backgroundTaskId")
    if bg_task_id:
        os.makedirs(STATE_DIR, exist_ok=True)
        job_path = os.path.join(STATE_DIR, f"bash-{bg_task_id}.json")
        job = {
            "task_id": bg_task_id,
            "description": desc,
            "session_id": session_id,
            "output_file": guess_output_file(session_id, bg_task_id),
            "start_epoch": time.time(),
            "threshold_seconds": THRESHOLD_SECONDS,
        }
        try:
            with open(job_path, "w") as f:
                json.dump(job, f)
            subprocess.Popen(
                ["python3", WATCHER, job_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass
        return

    duration_ms = d.get("duration_ms")
    if not isinstance(duration_ms, (int, float)):
        return
    if duration_ms / 1000.0 < THRESHOLD_SECONDS:
        return

    kind = "error" if tr.get("interrupted") else "done"
    minutes = round(duration_ms / 60000.0, 1)
    enqueue(session_id, f"Bash-команда (~{minutes} мин): {desc}", kind)


if __name__ == "__main__":
    main()
