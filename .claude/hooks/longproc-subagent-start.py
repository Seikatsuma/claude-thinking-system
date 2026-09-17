#!/usr/bin/env python3
# SubagentStart hook — фиксирует момент запуска субагента (Agent-тул, любой subagent_type,
# в т.ч. фоновые агенты и агенты внутри Workflow). Парный к longproc-subagent-stop.py.
import json
import os
import time

STATE_DIR = os.path.expanduser("~/.claude/hooks/state/longproc")


def main():
    try:
        d = json.loads(os.environ.get("HOOK_IN") or "{}")
    except Exception:
        return
    agent_id = d.get("agent_id")
    if not agent_id:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, f"subagent-{agent_id}.start")
    try:
        with open(path, "w") as f:
            json.dump({"start_epoch": time.time(), "agent_type": d.get("agent_type")}, f)
    except Exception:
        pass


if __name__ == "__main__":
    main()
