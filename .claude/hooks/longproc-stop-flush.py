#!/usr/bin/env python3
# Stop hook — единственное место, откуда реально уходит TG-уведомление о долгих
# процессах. Продюсеры (longproc-bash-notify.py, longproc-bg-watcher.py,
# longproc-subagent-stop.py) только кладут запись в очередь этой сессии.
#
# Флашим ТОЛЬКО когда сессия на момент Stop действительно "полностью перестала
# думать": нет ни запланированного продолжения (session_crons — например,
# ScheduleWakeup внутри /loop), ни живых фоновых задач (background_tasks — ещё не
# завершившийся Bash/субагент). Если что-то из этого не пусто — цикл ещё идёт,
# просто выходим, запись останется в очереди до следующего Stop.
#
# Причина: до 24.08.26 уведомление слали сразу в момент завершения каждого шага —
# Пользователь получал пинг за каждую часть многошагового цикла ("часть 13 цикла"). Хотел
# ровно одно сообщение — когда всё действительно закончилось.
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from longproc_common import NOTIFY, queue_path


def _verification_open(session_id):
    """True, если в реестре проверок есть пункты без вердикта (см. evidence-gate)."""
    if not session_id:
        return False
    try:
        state = os.path.expanduser(
            "~/.claude/hooks/state/verify/%s.json" % session_id)
        if not os.path.exists(state):
            return False
        with open(state) as f:
            st = json.load(f)
        checks = st.get("checks") or []
        if st.get("touched") and not [c for c in checks if c.get("verdict")]:
            return True
        return any(c.get("verdict") is None for c in checks)
    except Exception:
        return False  # сомнение толкуем в пользу отправки: молчание хуже лишнего пинга


def main():
    try:
        d = json.loads(os.environ.get("HOOK_IN") or "{}")
    except Exception:
        return

    if d.get("background_tasks") or d.get("session_crons"):
        return  # сессия ещё не полностью свободна — ждём следующего Stop

    # Незакрытая проверка = работа не закончена, даже если процессы отработали.
    # Иначе пользователь получит "готово" раньше, чем результат кто-то увидел на экране.
    if _verification_open(d.get("session_id")):
        return

    path = queue_path(d.get("session_id"))
    if not os.path.exists(path):
        return

    try:
        with open(path) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
    except Exception:
        return

    try:
        os.remove(path)
    except Exception:
        pass

    if not lines:
        return

    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    if not items:
        return

    if len(items) == 1:
        msg = items[0].get("text", "")
        kind = items[0].get("kind_tg", "done")
    else:
        bullets = "\n".join(f"• {it.get('text', '')}" for it in items)
        msg = f"Завершено {len(items)} долгих процессов:\n{bullets}"
        kind = "error" if any(it.get("kind_tg") == "error" for it in items) else "done"

    try:
        subprocess.run([NOTIFY, msg, kind], timeout=20)
    except Exception:
        pass


if __name__ == "__main__":
    main()
