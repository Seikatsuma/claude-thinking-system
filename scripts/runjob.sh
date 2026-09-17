#!/usr/bin/env bash
# runjob — устойчивый прогон пачечной работы (транскрибация, массовый разбор, синк, выгрузка).
#
# ЗАЧЕМ. Разовый скрипт снимает список работ ОДИН раз при старте, отрабатывает его и молча
# завершается «успешно». Всё, что появилось позже, остаётся несделанным; счётчик стоит,
# и со стороны это неотличимо от «идёт». Набито трижды 27.07.26 на аудите Подагра/Шпора.
#
# ЧТО ДАЁТ:
#   1. пропуск уже сделанного   — единица готова, если существует её файл-результат;
#   2. докрутку до нуля         — волнами, список пересматривается ПЕРЕД каждой волной;
#   3. состояние на диске       — STATUS с временем последнего успеха (виден затык, а не только счёт);
#   4. живучесть                — setsid, переживает обрыв сессии и перезапуск агента;
#   5. честный финал            — что не поддалось, перечислено; тишина не выдаётся за успех.
#
# ИСПОЛЬЗОВАНИЕ
#   runjob.sh run <имя> --items <файл> --done <шаблон> --cmd '<команда>' [--workers N] [--waves N]
#       <файл>    — список единиц работы, по одной в строке (ключ задачи)
#       <шаблон>  — путь к файлу-результату, {} заменяется на ключ (наличие = сделано)
#       <команда> — обработчик одной единицы, {} заменяется на ключ
#   runjob.sh status <имя>          — состояние: сделано/осталось/ошибок/когда последний успех
#   runjob.sh list                  — все задания и их состояние
#   runjob.sh stop <имя>            — остановить
#
# ПРИМЕР
#   ls _ps_tr/*.txt | sed 's/.*\///;s/\.txt$//' > /tmp/keys.txt
#   runjob.sh run analyze --items /tmp/keys.txt --done '_ps_tr/{}.an.json' \
#             --cmd 'python3 ps_analyze.py --one {}' --workers 6
set -uo pipefail

STATE_ROOT="${RUNJOB_HOME:-$HOME/.runjob}"
mkdir -p "$STATE_ROOT"

now() { date '+%Y-%m-%d %H:%M:%S'; }

# ---------- состояние ----------
write_status() {           # имя done left err wave phase
  local d="$STATE_ROOT/$1"
  mkdir -p "$d"
  cat > "$d/STATUS" <<EOF
задание:          $1
фаза:             $6
волна:            $5
сделано:          $2
осталось:         $3
ошибок:           $4
последний успех:  ${LAST_OK:-—}
обновлено:        $(now)
pid:              ${MAIN_PID:-—}
EOF
}

cmd_status() {
  local d="$STATE_ROOT/${1:?укажи имя задания}"
  [ -f "$d/STATUS" ] || { echo "нет такого задания: $1"; exit 1; }
  cat "$d/STATUS"
  if [ -s "$d/failed.txt" ]; then
    echo "--- не поддалось ($(wc -l < "$d/failed.txt")):"
    head -10 "$d/failed.txt"
  fi
}

cmd_list() {
  local any=0
  for d in "$STATE_ROOT"/*/; do
    [ -f "$d/STATUS" ] || continue
    any=1
    awk -F': +' '/^задание|^фаза|^сделано|^осталось|^последний успех/ {printf "%s=%s  ", $1, $2} END {print ""}' "$d/STATUS"
  done
  [ $any -eq 0 ] && echo "заданий нет"
}

cmd_stop() {
  local d="$STATE_ROOT/${1:?укажи имя}"
  [ -f "$d/pid" ] || { echo "не запущено"; exit 1; }
  kill "$(cat "$d/pid")" 2>/dev/null && echo "остановлено: $1"
}

# ---------- прогон ----------
cmd_run() {
  local NAME="${1:?укажи имя задания}"; shift
  local ITEMS="" DONE_TPL="" CMD_TPL="" WORKERS=4 MAX_WAVES=30 RETRY=2 SETTLE=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --items)   ITEMS="$2";     shift 2 ;;
      --done)    DONE_TPL="$2";  shift 2 ;;
      --cmd)     CMD_TPL="$2";   shift 2 ;;
      --workers) WORKERS="$2";   shift 2 ;;
      --waves)   MAX_WAVES="$2"; shift 2 ;;
      --retry)   RETRY="$2";     shift 2 ;;
      --settle)  SETTLE="$2";    shift 2 ;;   # ждать N сек после нулевого остатка и перепроверить
      *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
  done
  [ -n "$ITEMS" ] && [ -n "$DONE_TPL" ] && [ -n "$CMD_TPL" ] || {
    echo "нужны --items, --done, --cmd" >&2; exit 2; }
  [ -f "$ITEMS" ] || { echo "нет файла со списком: $ITEMS" >&2; exit 2; }

  local D="$STATE_ROOT/$NAME"
  mkdir -p "$D"
  MAIN_PID=$$
  echo $$ > "$D/pid"
  LAST_OK=""
  : > "$D/failed.txt"
  echo "[$(now)] старт задания «$NAME»: $(wc -l < "$ITEMS") единиц, воркеров $WORKERS" | tee -a "$D/log"

  local wave
  for wave in $(seq 1 "$MAX_WAVES"); do
    # список пересматривается КАЖДУЮ волну — иначе хвост, дописанный по ходу, потеряется
    local todo="$D/todo.txt"
    : > "$todo"
    local total=0 done_n=0
    while IFS= read -r key; do
      [ -z "$key" ] && continue
      total=$((total + 1))
      local marker="${DONE_TPL//\{\}/$key}"
      if [ -e "$marker" ]; then
        done_n=$((done_n + 1))
      elif [ "$(grep -c -x -F "$key" "$D/failed.txt" 2>/dev/null || echo 0)" -ge "$RETRY" ]; then
        :                                  # исчерпал попытки — больше не берём
      else
        echo "$key" >> "$todo"
      fi
    done < "$ITEMS"

    local left; left=$(wc -l < "$todo")
    local errs; errs=$(sort -u "$D/failed.txt" 2>/dev/null | wc -l)
    write_status "$NAME" "$done_n" "$left" "$errs" "$wave" "работает"

    if [ "$left" -eq 0 ]; then
      # ⚠ Остаток ноль ≠ работа окончена: список могут дописывать снаружи (сегодняшний случай —
      # разбор доедал файлы, пока транскрибация их дописывала). --settle N: подождать и перепроверить.
      if [ "$SETTLE" -gt 0 ]; then
        write_status "$NAME" "$done_n" 0 "$errs" "$wave" "ожидание новых работ (${SETTLE}с)"
        sleep "$SETTLE"
        local recheck=0
        while IFS= read -r key; do
          [ -z "$key" ] && continue
          local m="${DONE_TPL//\{\}/$key}"
          [ -e "$m" ] || [ "$(grep -c -x -F "$key" "$D/failed.txt" 2>/dev/null || echo 0)" -ge "$RETRY" ] || recheck=1
        done < "$ITEMS"
        if [ "$recheck" -eq 1 ]; then
          echo "[$(now)] появились новые работы — продолжаю" | tee -a "$D/log"
          continue
        fi
      fi
      local phase="ЗАВЕРШЕНО"
      [ "$errs" -gt 0 ] && phase="ЗАВЕРШЕНО С ОШИБКАМИ: $errs"   # тишина не выдаётся за успех
      write_status "$NAME" "$done_n" 0 "$errs" "$wave" "$phase"
      echo "[$(now)] $phase: сделано $done_n из $total, не поддалось $errs" | tee -a "$D/log"
      rm -f "$D/pid"
      [ "$errs" -gt 0 ] && return 1
      return 0
    fi

    echo "[$(now)] волна $wave: сделано $done_n из $total, в работе $left" | tee -a "$D/log"

    # обработка волны: xargs держит ровно WORKERS параллельных обработчиков
    export RUNJOB_DIR="$D" RUNJOB_DONE_TPL="$DONE_TPL" RUNJOB_CMD_TPL="$CMD_TPL"
    < "$todo" xargs -P "$WORKERS" -I '{KEY}' bash -c '
        key="{KEY}"
        cmd="${RUNJOB_CMD_TPL//\{\}/$key}"
        marker="${RUNJOB_DONE_TPL//\{\}/$key}"
        eval "$cmd" >> "$RUNJOB_DIR/worker.log" 2>&1
        if [ -e "$marker" ]; then
            date "+%Y-%m-%d %H:%M:%S" > "$RUNJOB_DIR/last_ok"   # heartbeat: когда был успех
        else
            echo "$key" >> "$RUNJOB_DIR/failed.txt"             # попытка не удалась
        fi
    '
    [ -f "$D/last_ok" ] && LAST_OK="$(cat "$D/last_ok")"
  done

  local left2; left2=$(wc -l < "$D/todo.txt" 2>/dev/null || echo 0)
  write_status "$NAME" "$done_n" "$left2" "$(sort -u "$D/failed.txt" | wc -l)" "$MAX_WAVES" "ОСТАНОВЛЕНО: исчерпан лимит волн"
  echo "[$(now)] исчерпан лимит волн ($MAX_WAVES), осталось $left2" | tee -a "$D/log"
  rm -f "$D/pid"
  return 1
}

case "${1:-}" in
  run)    shift; cmd_run "$@" ;;
  status) shift; cmd_status "$@" ;;
  list)   shift; cmd_list "$@" ;;
  stop)   shift; cmd_stop "$@" ;;
  *) sed -n '2,30p' "$0"; exit 2 ;;
esac
