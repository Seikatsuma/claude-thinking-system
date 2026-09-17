#!/bin/bash
# Лёгкий watchdog для VPS (3.8 ГБ, 2 CPU).
# Задача: заметить перегруз ДО того, как сервер станет недоступен по SSH.
# Запускается из crontab каждые 5 минут. Runtime ~2 с (проверки 6-7 опрашивают
# журнал каждой живой службы; на фоне пятиминутного интервала это ничто).
# Алерты идут в Telegram-бот оповещений (тот же, что для отчётов).
# Дебаунс: одна и та же проблема не шлётся чаще раз в 30 минут.

set -u

# State в ~/.local/state — переживает reboot и не чистится systemd-tmpfiles,
# в отличие от /tmp (где чистка = ложные алерты).
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/watchdog"
mkdir -p "$STATE_DIR"

# Пороги. Подобраны консервативно для 3.8 ГБ / 2 CPU.
LOAD_CRIT=6            # load average > 6 на 2 ядрах = страдание
SWAP_CRIT_MB=3000      # > 3 ГБ свопа = thrashing близко
DISK_CRIT=93           # диск > 93% = приближается смерть
CPU_HOG_PCT=80         # процесс > 80% CPU
CPU_HOG_MIN=3          # держится дольше 3 минут = аномалия
RESTART_DELTA=5        # прирост рестартов юнита между запусками watchdog = цикл
MEM_NEAR_LIMIT_PCT=75  # служба заняла столько % своего потолка = предупредить ДО падения

DEBOUNCE_SEC=1800      # 30 минут между повторами одного алерта

# Токен алертного бота — из ~/.secrets/notify.env (chmod 600). Fail-fast если файла нет.
NOTIFY_ENV="$HOME/.secrets/notify.env"
if [ ! -f "$NOTIFY_ENV" ]; then
    echo "watchdog.sh: не найден $NOTIFY_ENV — алерты не пойдут" >&2
    exit 0
fi
# shellcheck disable=SC1090
source "$NOTIFY_ENV"
TG_TOKEN="${NOTIFY_TG_TOKEN:-}"
TG_CHAT="${NOTIFY_TG_CHAT:-}"
if [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ]; then
    echo "watchdog.sh: в $NOTIFY_ENV нет NOTIFY_TG_TOKEN или NOTIFY_TG_CHAT" >&2
    exit 0
fi

alert() {
    local key="$1" msg="$2"
    local stamp_file="$STATE_DIR/last_${key}"
    local now=$(date +%s)

    if [ -f "$stamp_file" ]; then
        local last=$(cat "$stamp_file")
        [ $((now - last)) -lt "$DEBOUNCE_SEC" ] && return
    fi

    echo "$now" > "$stamp_file"

    local host=$(hostname -s)
    local full="🚨 ${host} watchdog\n\n${msg}\n\n$(date '+%Y-%m-%d %H:%M:%S %Z')"

    curl -s --max-time 5 \
         -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
         -d chat_id="$TG_CHAT" \
         -d text="$(echo -e "$full")" \
         -d parse_mode="HTML" > /dev/null 2>&1
}

# 1) Load average (1-минутный)
load=$(awk '{print $1}' /proc/loadavg)
load_int=${load%.*}
if [ "$load_int" -ge "$LOAD_CRIT" ]; then
    top=$(ps -eo pid,%cpu,%mem,cmd --sort=-%cpu --no-headers 2>/dev/null | head -3 | awk '{$1=$1};1')
    alert "load" "Load average <b>${load}</b> (порог ${LOAD_CRIT})\n\nТоп CPU:\n<code>${top}</code>"
fi

# 2) Swap использование
swap_used_kb=$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print t-f}' /proc/meminfo)
swap_used_mb=$((swap_used_kb / 1024))
if [ "$swap_used_mb" -ge "$SWAP_CRIT_MB" ]; then
    top=$(ps -eo pid,%mem,rss,cmd --sort=-%mem --no-headers 2>/dev/null | head -3 | awk '{$1=$1};1')
    alert "swap" "Swap <b>${swap_used_mb} МБ</b> (порог ${SWAP_CRIT_MB})\n\nТоп RAM:\n<code>${top}</code>"
fi

# 3) Диск
disk_pct=$(df / --output=pcent | tail -1 | tr -dc '0-9')
if [ "${disk_pct:-0}" -ge "$DISK_CRIT" ]; then
    alert "disk" "Диск / занят <b>${disk_pct}%</b> (порог ${DISK_CRIT}%)"
fi

# 4) Процесс-обжора CPU дольше N минут
STATE_CPU="$STATE_DIR/cpu_hog_start"
hog=$(ps -eo pid,%cpu,cmd --sort=-%cpu --no-headers 2>/dev/null | head -1)
hog_pct=$(echo "$hog" | awk '{print $2}' | cut -d. -f1)
hog_pid=$(echo "$hog" | awk '{print $1}')
if [ "${hog_pct:-0}" -ge "$CPU_HOG_PCT" ]; then
    if [ -f "$STATE_CPU" ]; then
        prev_pid=$(awk '{print $1}' "$STATE_CPU")
        prev_start=$(awk '{print $2}' "$STATE_CPU")
        now=$(date +%s)
        if [ "$prev_pid" = "$hog_pid" ] && [ $((now - prev_start)) -ge $((CPU_HOG_MIN * 60)) ]; then
            alert "cpu_hog_${hog_pid}" "Процесс жрёт CPU >${CPU_HOG_PCT}% дольше ${CPU_HOG_MIN} мин:\n<code>${hog}</code>"
        elif [ "$prev_pid" != "$hog_pid" ]; then
            echo "$hog_pid $(date +%s)" > "$STATE_CPU"
        fi
    else
        echo "$hog_pid $(date +%s)" > "$STATE_CPU"
    fi
else
    rm -f "$STATE_CPU"
fi

# 5) Systemd юниты в цикле auto-restart — смотрим ПРИРОСТ рестартов между запусками.
# NRestarts копится с момента boot, важен именно рост за последние 5 мин.
for unit_file in /etc/systemd/system/*.service; do
    [ -f "$unit_file" ] || continue
    unit=$(basename "$unit_file")
    n_now=$(systemctl show "$unit" -p NRestarts --value 2>/dev/null)
    [ -z "$n_now" ] && continue
    state_file="$STATE_DIR/nrestarts_${unit}"
    # Warm-up: если state отсутствует (первый запуск / потеря файла / после reboot) —
    # только сохраняем baseline и НЕ алертим. Иначе получаем ложные тревоги
    # на "исторически накопленные" NRestarts, которые уже давно не проблема.
    if [ ! -f "$state_file" ]; then
        echo "$n_now" > "$state_file"
        continue
    fi
    n_prev=$(cat "$state_file")
    echo "$n_now" > "$state_file"
    delta=$((n_now - n_prev))

    # Служба, чей процесс живёт дольше окна наблюдения, не может быть в цикле
    # перезапусков — что бы ни говорил счётчик.
    #
    # Счётчик копится с момента загрузки сервера и обнуляется только вместе с
    # ним. Стоит файлу с прошлым значением потеряться или устареть (потеря
    # состояния, ручной запуск сторожа, чистка) — и вся история за недели
    # засчитывается как «прирост за пять минут». 09.09 так пришла тревога о
    # 2164 перезапусках одного из ботов, чей процесс на тот момент работал без
    # единого сбоя пятнадцать суток. Тревога стоила бы живого бота: пользователь уже
    # заносил руку, чтобы его убить.
    #
    # Возраст процесса — факт, а не оценка, и проверяется он до отправки.
    now_us=$(awk '{printf "%d", $1 * 1000000}' /proc/uptime)
    start_us=$(systemctl show "$unit" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)
    case "$start_us" in ''|*[!0-9]*) start_us=0;; esac
    if [ "$start_us" -gt 0 ]; then
        age_sec=$(( (now_us - start_us) / 1000000 ))
        if [ "$age_sec" -gt 300 ]; then
            continue
        fi
    fi

    if [ "$delta" -ge "$RESTART_DELTA" ]; then
        alert "restart_${unit}" "Юнит <b>${unit}</b> перезапустился <b>${delta}</b> раз за последние 5 мин (всего с загрузки: ${n_now}).\nСмотри: <code>journalctl -u ${unit} -n 30 --no-pager</code>"
    fi
done

# 6) Служба подошла к своему потолку памяти.
#
# Проверки 1-5 срабатывают, когда беда уже случилась: сервер в свопе, юнит в
# цикле рестартов. Эта — раньше. 09.09 общая служба claudecodeui подошла к
# потолку и упёрлась в него, и первым сигналом стал уже круг перезапусков.
# Занятые три четверти отведённой памяти — это ещё работающая служба, но
# запас кончается, и об этом стоит узнать заранее.
#
# 7) Прямой след нехватки памяти в журнале службы.
#
# Самый недвусмысленный сигнал: процессу не хватило памяти. Ловим и падение
# по куче внутри процесса (JavaScript heap out of memory), и убийство
# процесса системой (oom-kill) — по симптому, а не по догадке.
for unit_file in /etc/systemd/system/*.service; do
    [ -f "$unit_file" ] || continue
    unit=$(basename "$unit_file")
    [ "$(systemctl is-active "$unit" 2>/dev/null)" = "active" ] || continue

    mem_max=$(systemctl show "$unit" -p MemoryMax --value 2>/dev/null)
    mem_cur=$(systemctl show "$unit" -p MemoryCurrent --value 2>/dev/null)
    case "$mem_max" in ''|infinity|*[!0-9]*) mem_max="";; esac
    case "$mem_cur" in ''|infinity|*[!0-9]*) mem_cur="";; esac

    if [ -n "$mem_max" ] && [ -n "$mem_cur" ] && [ "$mem_max" -gt 0 ]; then
        pct=$((mem_cur * 100 / mem_max))
        if [ "$pct" -ge "$MEM_NEAR_LIMIT_PCT" ]; then
            alert "mem_${unit}" "Служба <b>${unit}</b> заняла <b>${pct}%</b> отведённой ей памяти ($((mem_cur / 1048576)) из $((mem_max / 1048576)) МБ).\nЗапас кончается — при следующем всплеске служба упадёт.\nСмотри: <code>journalctl -u ${unit} -n 30 --no-pager</code>"
        fi
    fi

    if journalctl -u "$unit" --since "5 min ago" --no-pager -q 2>/dev/null \
        | grep -qE "heap out of memory|Out of memory|oom-kill|Killed process"; then
        alert "oom_${unit}" "Службе <b>${unit}</b> не хватило памяти (след в журнале за последние 5 минут).\nПадает не сервер, а одна служба — но она уйдёт в перезапуск и потянет за собой остальных.\nСмотри: <code>journalctl -u ${unit} -n 40 --no-pager</code>"
    fi
done

# 8) Включённая служба пользователя, которая не работает.
#
# Проверки 5-7 смотрят только в /etc/systemd/system — то есть на службы,
# поставленные через sudo. Между тем боты пользователя живут пользовательскими
# юнитами (~/.config/systemd/user), и туда сторож не заглядывал вовсе.
# 10.09 corrector-bot вышел с кодом 0 — при Restart=on-failure systemd такой
# выход перезапуском не считает, и бот молча простоял двое суток, пока пользователь
# сам не заметил, что тот не отвечает.
#
# Ловим сам симптом: служба включена в автозапуск (значит должна работать), а
# не работает. Ничего не поднимаем — только сообщаем: перезапуск чужого бота
# вслепую хуже, чем его простой.
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
systemctl --user list-unit-files --state=enabled --no-pager -q 2>/dev/null \
    | awk '$1 ~ /\.service$/ {print $1}' \
    | while read -r unit; do
        state=$(systemctl --user is-active "$unit" 2>/dev/null)
        [ "$state" = "active" ] && continue
        [ "$state" = "activating" ] && continue
        alert "userunit_${unit}" "Служба <b>${unit}</b> включена в автозапуск, но не работает (состояние: <b>${state:-неизвестно}</b>).\nЕсли это бот — он сейчас не отвечает.\nСмотри: <code>journalctl --user -u ${unit} -n 30 --no-pager</code>\nПоднять: <code>systemctl --user start ${unit}</code>"
    done

exit 0
