#!/usr/bin/env python3
"""UserPromptSubmit: считает сообщения пользователя в чате и подсказывает поднять тему.

Молчит почти всегда — ничего не подкладывает в обычные сообщения (служебного текста в
каждом сообщении и так ~3.5 тыс. знаков, засорять контекст нельзя). Говорит в двух случаях:
чат без темы, а пользователь продолжает прошлое дело; или в теме второй раз «ты забыл, что мы решили».
Счётчик сообщений нужен topic-gate.py. Любая ошибка — молча пропускаем (fail-open).
Устройство — ~/scripts/topics/agent.md.
"""
import importlib.util, json, os, re, sys

TOPIC = os.path.expanduser("~/scripts/topics/topic.py")
CONTINUE = re.compile(
    r"продолж\w*\s+(?:про|по\s|тем|работ|разговор|с\s|наш|то\b|начат)|верн[её]м(?:ся)?\s+к|вернуться\s+к|"
    r"помнишь|что\s+(?:у\s+нас\s+)?открыто|какие\s+(?:у\s+нас\s+)?(?:есть\s+)?темы|на\s+ч[её]м\s+(?:мы\s+)?останов|"
    r"подними\s+тем|подтяни\s+(?:тем|контекст)|из\s+(?:прошлого|того|старого|другого)\s+чата")
# Тот же список, что в task-framing.sh (пояснения маркеров — там). Менять в обоих местах.
SERVICE_MARKERS = ("<task-notification", "<task-id>", "<system", "Stop hook feedback",
                   "Stop hook blocking error", "[SYSTEM NOTIFICATION", "[MESSAGE FROM NON-USER",
                   "This session is being continued", "Another Claude session sent a message",
                   "<command-", "<local-command", "[Your previous response", "Base directory for this skill",
                   "[Request interrupted", "Continue from where you left off")
MISS = re.compile(r"ты\s+(?:же\s+)?забыл|мы\s+(?:же\s+)?(?:это\s+)?(?:уже\s+)?(?:решили|договорил|обсуждал)|не\s+помнишь")


def main():
    d = json.loads(os.environ.get("HOOK_IN") or sys.stdin.read() or "{}")
    sess = d.get("session_id") or ""
    prompt = (d.get("prompt") or "").strip()
    if not sess or not prompt or prompt.startswith("/"):
        return
    # 14.09.26: уведомления фоновых задач и отзывы Stop-хуков — не сообщения пользователя, счётчик не трогаем.
    head = prompt[:500]
    if any(mk in head for mk in SERVICE_MARKERS) or (prompt.startswith("{") and '"type"' in head):
        return
    spec = importlib.util.spec_from_file_location("topic", TOPIC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    st = m.load_state(sess)
    st["session_id"] = sess
    st["transcript"] = d.get("transcript_path") or st.get("transcript", "")
    st["cwd"] = d.get("cwd") or st.get("cwd", "")
    st["prompts"] = st.get("prompts", 0) + 1
    st["last_prompt"] = m.ts()
    m.save_state(sess, st)

    low = prompt.lower().replace("ё", "е")
    topic = st.get("topic")
    parts = []
    if topic and MISS.search(low) and m.add_miss(topic) >= 2:
        parts.append("ТЕМА «%s»: пользователь уже не первый раз говорит, что ты забыл решённое. Значит, страница "
                     "темы теряет важное. Кроме ответа — найди, чего на ней не было, допиши (topic.py decide / "
                     "mode) и поправь правило записи в ~/scripts/topics/agent.md." % topic)
    if not topic and CONTINUE.search(low):
        parts.append("ПОДЪЁМ ТЕМЫ. Пользователь продолжает дело из прошлого чата. До ответа выполни:\n"
                     "  python3 ~/scripts/topics/topic.py open \"<суть из сообщения>\" --session %s\n"
                     "(все темы: topic.py list). Первой строкой ответа — какую тему поднял и на чём остановились. "
                     "Несколько похожих — один закрытый вопрос пользователю. Не нашлось — скажи прямо, не "
                     "восстанавливай дело по догадке." % sess)
    if parts:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n\n".join(parts)}}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
