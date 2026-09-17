#!/usr/bin/env python3
# Отсоединённый вотчер для фонового Bash (run_in_background). Запускается из
# longproc-bash-notify.py через Popen(start_new_session=True) и переживает hook-таймаут
# и, если нужно, конец текущего хода — работает независимо от того, жива ли сессия.
# Следит за сентинелом "[exited with code N]" / "[killed]" / "[timed out]" в конце
# output-файла задачи; как только команда реально завершилась и заняла больше порога —
# кладёт запись в очередь (см. longproc_common.py) — реально уходит в TG только на
# Stop, когда сессия полностью освободилась, не за отдельную часть цикла.
# Есть защитный потолок ожидания, чтобы вотчер не завис навечно, если output-файл
# почему-то не найден или сентинел так и не появился.
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from longproc_common import enqueue

POLL_SECONDS = 15
MAX_WAIT_SECONDS = 6 * 60 * 60

SENTINEL_RE = re.compile(r"^\[(exited with code (\d+)|killed|timed out)")


def find_sentinel(output_file):
    try:
        size = os.path.getsize(output_file)
        with open(output_file, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if line.startswith("["):
            m = SENTINEL_RE.match(line)
            if m:
                return m
    return None


def main():
    if len(sys.argv) < 2:
        return
    job_path = sys.argv[1]
    try:
        with open(job_path) as f:
            job = json.load(f)
    except Exception:
        return

    output_file = job.get("output_file")
    start_epoch = job.get("start_epoch") or time.time()
    threshold = job.get("threshold_seconds") or 600
    desc = job.get("description") or "фоновая команда"
    session_id = job.get("session_id") or ""

    exit_code = None
    waited = 0
    if output_file:
        while waited < MAX_WAIT_SECONDS:
            m = find_sentinel(output_file)
            if m:
                exit_code = int(m.group(2)) if m.group(2) is not None else 1
                break
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
    else:
        time.sleep(threshold)

    elapsed = time.time() - start_epoch
    try:
        os.remove(job_path)
    except Exception:
        pass

    if elapsed < threshold:
        return

    minutes = round(elapsed / 60.0, 1)
    kind = "done" if exit_code in (None, 0) else "error"
    enqueue(session_id, f"Фоновая команда (~{minutes} мин): {desc}", kind)


if __name__ == "__main__":
    main()
