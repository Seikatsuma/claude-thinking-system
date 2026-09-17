#!/usr/bin/env bash
# StopFailure — чат не смог ответить из-за сбоя Claude (истёк вход, лимит, перегрузка).
# 12.09.26 вход истёк посреди работы, и пользователь 5 часов писал «ну?», «а?», «хай» в пустоту:
# модель в этот момент ответить не может, сообщить может только хук. Шлёт в Telegram через
# notify.sh — не чаще раза в 30 минут на один вид сбоя в одной сессии.
set +e
HOOK_IN="$(cat 2>/dev/null)" python3 - <<'PY' 2>/dev/null
import os, json, sys, time, subprocess, hashlib

try:
    d = json.loads(os.environ.get("HOOK_IN") or "{}")
except Exception:
    sys.exit(0)
et = d.get("error_type") or "unknown"
if et == "max_output_tokens":
    sys.exit(0)  # ответ просто упёрся в длину — чат жив
sess = d.get("session_id") or "?"
where = os.path.basename((d.get("cwd") or "").rstrip("/")) or "штаб"
PLAIN = {
    "authentication_failed": ("Вход в Claude истёк — этот чат не может отвечать.",
                              "набрать /login в окне этого аккаунта; до этого сообщения останутся без ответа."),
    "cloud_credential_error": ("Чат не смог прочитать ключ входа в Claude.",
                               "набрать /login в окне этого аккаунта."),
    "oauth_org_not_allowed": ("Вход в Claude не пускает эту организацию.", "войти другим аккаунтом через /login."),
    "account_on_hold": ("Аккаунт Claude приостановлен.", "проверить аккаунт на claude.ai."),
    "billing_error": ("Проблема с оплатой подписки Claude.", "проверить оплату на claude.ai."),
    "rate_limit": ("Упёрся в лимит подписки Claude.", "чат ответит после сброса лимита; срочное — в другом аккаунте."),
    "overloaded": ("Сервис Claude перегружен.", "повторить сообщение через пару минут."),
    "server_error": ("Сбой на стороне Claude.", "повторить сообщение через пару минут."),
    "model_not_found": ("Выбранная модель недоступна.", "сменить модель в этом чате."),
    "invalid_request": ("Claude не принял запрос.", "повторить сообщение; если повторится — написать в другом чате."),
}
what, todo = PLAIN.get(et, ("Чат не смог ответить из-за сбоя Claude.", "повторить сообщение через пару минут."))
marks = os.path.expanduser("~/.claude/hooks/state/verify/failures")
os.makedirs(marks, exist_ok=True)
mark = os.path.join(marks, hashlib.md5((sess + et).encode()).hexdigest()[:16])
try:
    if time.time() - os.path.getmtime(mark) < 1800:
        sys.exit(0)
except OSError:
    pass
text = "⚠️ %s\nЧто сделать: %s\nГде: %s. Причина: %s" % (
    what, todo, where, (d.get("error_message") or et)[:160])
if os.environ.get("VERIFY_NOTIFY_DRY"):
    print(text)
else:
    subprocess.run([os.path.expanduser("~/scripts/notify.sh"), text, "error"], timeout=20)
open(mark, "w").write(str(time.time()))
sys.exit(0)
PY
exit 0
