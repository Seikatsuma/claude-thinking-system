#!/usr/bin/env python3
"""Темы между чатами: страница состояния дела, которую подхватывает любой новый чат.

Зачем (13.09.26, пользователь): чат тупеет и ест лимит, если его не сбрасывать, а после сброса
теряется ход дела — пользователь просил «выведи память и перенеси» руками. Здесь у дела есть
страница из трёх блоков (режим работы / сейчас / решения), она копится по ходу разговора
и поднимается в новом чате по запросу. Устройство, пороги и грабли — agent.md рядом.
"""
import argparse, datetime as dt, difflib, fcntl, glob, json, os, re, shutil, sys
from contextlib import contextmanager

HOME = os.path.expanduser("~")
ROOT = os.environ.get("TOPICS_ROOT") or os.path.join(HOME, "vault", "topics")
STATE = os.environ.get("TOPICS_STATE") or os.path.join(HOME, ".claude", "hooks", "state", "topics")
ARCHIVE = os.path.join(ROOT, "_archive")
TO_MEMORY = os.path.join(ROOT, "_to-memory.jsonl")

NOW_MAX = 4000          # «Сейчас» — одна страница: упёрлись — ужать, а не дописать
MODE_MAX = 1500
DECISIONS_MAX = 6000    # больше — сжать сводкой, полный текст уходит в архив темы
ARCHIVE_AFTER_DAYS = 21
# Удаление — по постоянному разрешению пользователя от 13.09.26: только архивные темы, 90 дней
# без обращений, выводы для долгой памяти перенесены. Всё прочее — как раньше, только с «да».
DELETE_AFTER_DAYS = 90
RECENT_ARCHIVE_DAYS = 30
# Отставание чата от страницы считается по сообщениям пользователя после записи (счётчик хука);
# по времени — только для чатов без счётчика. 120 с давали «ОТСТАЁТ» почти на каждом последнем ответе.
LAG_SECONDS = 1800
# Раздел чата, который говорил недавно, вбирать нельзя: 13.09 живой тест вобрал раздел
# соседнего работающего чата, и его «где мы» ушло в архив темы. Ручная уборка — TOPICS_FORCE_ABSORB=1.
LIVE_HOURS = 2

# Ключ на странице темы уехал бы в GitHub вместе с vault. Указатель «где лежит» (~/…, /…, $…) можно.
SECRET = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|\b\d{8,10}:[A-Za-z0-9_-]{35}\b|"
    r"eyJ[A-Za-z0-9_-]{15,}\.[A-Za-z0-9_-]{15,}\.|"
    r"(?i:(?:password|passwd|пароль|secret|token|токен|api[_-]?key|ключ)\s*[:=]\s*(?![~/$])[^\s,;]{8,})")

TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
              ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
               "s", "t", "u", "f", "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))
STOP = {"pro", "po", "o", "ob", "v", "s", "i", "na", "k", "chto", "my", "mne", "nash", "nashu", "nashey",
        "tema", "temu", "temy", "teme", "prodolzhaem", "prodolzhim", "prodolzhit", "davay", "vernemsya",
        "pomnish", "etu", "eto", "tot", "tu", "chat", "chata", "chate", "razgovor", "delo", "u", "nas"}
SEC = re.compile(r"<!-- chat:(\w+) -->\n(.*?)<!-- /chat -->\n?", re.S)


# ---------- общее ----------
def now():
    return dt.datetime.now().replace(microsecond=0)


def ts():
    return now().isoformat()


def parse(iso):
    try:
        return dt.datetime.fromisoformat(iso)
    except Exception:
        return None


def human(iso):
    d = parse(iso) if isinstance(iso, str) else iso
    return d.strftime("%d.%m %H:%M") if d else "—"


def label(sess):
    return (sess or "")[:8]


def translit(s):
    return "".join(TR.get(ch, ch) for ch in (s or "").lower().replace("ё", "е"))


def slugify(s):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", translit(s))).strip("-")[:60]


def die(msg, code=1):
    print(msg)
    sys.exit(code)


def read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write(p, text):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def check_secret(text):
    m = SECRET.search(text or "")
    if m:
        die("ОТКАЗ: похоже на секрет («%s…»). На странице темы — только где лежит ключ "
            "(например ~/.secrets/имя.env), не сам ключ. Ничего не записано." % m.group(0)[:10], 5)


# ---------- состояние чата (общее с хуками) ----------
def state_path(sess):
    return os.path.join(STATE, "sess-%s.json" % sess)


def load_state(sess):
    try:
        with open(state_path(sess), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(sess, st):
    os.makedirs(STATE, exist_ok=True)
    write(state_path(sess), json.dumps(st, ensure_ascii=False, indent=1))


def find_transcript(sess):
    p = load_state(sess).get("transcript")
    if p and os.path.exists(p):
        return p
    for g in glob.glob(os.path.join(HOME, ".claude*", "projects", "*", "%s.jsonl" % sess)):
        return g
    return ""


def mark_written(sess):
    st = load_state(sess)
    st["prompts_at_last_write"] = st.get("prompts", 0)
    st.pop("squeeze_blocked", None)
    save_state(sess, st)


# ---------- темы ----------
@contextmanager
def locked(slug):
    os.makedirs(STATE, exist_ok=True)
    with open(os.path.join(STATE, "lock-%s" % slug), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def topic_dir(slug):
    for base, arch in ((ROOT, False), (ARCHIVE, True)):
        p = os.path.join(base, slug)
        if slug and not slug.startswith("_") and os.path.isfile(os.path.join(p, "meta.json")):
            return p, arch
    return None, None


def load_meta(d):
    with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
        return json.load(f)


def save_meta(d, meta):
    write(os.path.join(d, "meta.json"), json.dumps(meta, ensure_ascii=False, indent=1))


def all_topics():
    for base, arch in ((ROOT, False), (ARCHIVE, True)):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if not name.startswith("_") and os.path.isfile(os.path.join(p, "meta.json")):
                yield name, p, arch, load_meta(p)


def restore(slug):
    """Тему из архива вспомнили — возвращается целиком."""
    d, arch = topic_dir(slug)
    if arch:
        dst = os.path.join(ROOT, slug)
        shutil.move(d, dst)
        meta = load_meta(dst)
        meta["archived_at"] = None
        save_meta(dst, meta)
        return dst, True
    return d, False


def touch(d, meta):
    meta["last_touched"] = ts()
    save_meta(d, meta)


def bind(slug, sess):
    d, _ = restore(slug)
    meta = load_meta(d)
    lab = label(sess)
    s = meta.setdefault("sessions", {}).setdefault(lab, {})
    s.update({"session_id": sess, "transcript": find_transcript(sess), "bound": s.get("bound") or ts()})
    touch(d, meta)
    st = load_state(sess)
    st.update({"session_id": sess, "topic": slug, "oneoff": False})
    st["prompts_at_last_write"] = st.get("prompts", 0)
    save_state(sess, st)
    return d, meta


def current_topic(args):
    slug = args.topic or load_state(args.session).get("topic")
    if not slug:
        die("Чат не привязан к теме: topic.py open \"…\" --session S или topic.py new … --session S", 2)
    d, _ = topic_dir(slug)
    if not d:
        die("Темы «%s» нет. Все темы: topic.py list" % slug, 2)
    return slug


def read_sections(d):
    return [(m.group(1), m.group(2)) for m in SEC.finditer(read(os.path.join(d, "now.md")))]


def render_now(title, secs):
    return "# Сейчас — %s\n\n" % title + "\n".join(
        "<!-- chat:%s -->\n%s\n<!-- /chat -->\n" % (lab, body.rstrip()) for lab, body in secs)


def stopped_at(d):
    secs = read_sections(d)
    if not secs:
        return "страница пустая"
    lines = [l.strip(" -*#\t") for l in secs[-1][1].splitlines()]
    lines = [l for l in lines if l and not l.startswith("Чат ")]
    # «остановились» = следующий шаг, если он записан; заголовки вида «Где мы (13.09):» не показываем
    for i, l in enumerate(lines):
        if "дальше" in l.lower() and l.endswith(":") and i + 1 < len(lines):
            return lines[i + 1].lstrip("0123456789. ")[:110]
    body = [l for l in lines if not l.endswith(":")]
    return (body[0] if body else "—")[:110]


def pending_memory(slug=None):
    out = []
    for line in read(TO_MEMORY).splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        if not o.get("done") and (slug is None or o.get("topic") == slug):
            out.append(o)
    return out


def decisions_size(slug):
    d, _ = topic_dir(slug)
    return len(read(os.path.join(d, "decisions.md"))) if d else 0


def add_miss(slug):
    d, _ = topic_dir(slug)
    if not d:
        return 0
    meta = load_meta(d)
    meta["misses"] = meta.get("misses", 0) + 1
    save_meta(d, meta)
    return meta["misses"]


# ---------- поиск темы (голос искажает названия) ----------
def words(s):
    return [w for w in re.findall(r"[a-z0-9]+", translit(s)) if w not in STOP]


def fuzzy_word(a, b):
    return a == b or (len(a) >= 4 and len(b) >= 4 and a[:5] == b[:5]) or \
        difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def score(query, meta):
    q = words(query)
    if not q:
        return 0.0
    best = 0.0
    for cand in [meta["slug"], meta.get("title", "")] + meta.get("aliases", []):
        c = words(cand)
        if not c:
            continue
        hits = sum(1 for w in c if any(fuzzy_word(w, x) for x in q))
        ratio = difflib.SequenceMatcher(None, " ".join(q), " ".join(c)).ratio()
        best = max(best, hits / len(c), ratio)
    return round(best, 2)


def ranked(query):
    return sorted(((score(query, meta), slug, arch, meta) for slug, _, arch, meta in all_topics()),
                  key=lambda x: -x[0])


# ---------- команды ----------
def cmd_new(a):
    slug = slugify(a.slug)
    if not slug:
        die("Пустой слаг", 2)
    if topic_dir(slug)[0]:
        die("Тема «%s» уже есть — topic.py open %s --session S" % (slug, slug), 2)
    check_secret(a.title + " " + (a.aliases or ""))
    d = os.path.join(ROOT, slug)
    os.makedirs(d)
    meta = {"slug": slug, "title": a.title, "created": ts(), "last_touched": ts(), "archived_at": None,
            "aliases": [x.strip() for x in (a.aliases or "").split(",") if x.strip()], "sessions": {}, "misses": 0}
    save_meta(d, meta)
    write(os.path.join(d, "mode.md"), "# Режим работы — %s\n\n(не заполнен)\n" % a.title)
    write(os.path.join(d, "now.md"), render_now(a.title, []))
    write(os.path.join(d, "decisions.md"),
          "# Решения — %s\n\nДата · что выбрали — почему; отклонённое строкой ниже. Только дописывается.\n\n" % a.title)
    if a.session:
        bind(slug, a.session)
    print("ок: тема «%s» (%s) заведена%s. Заполни: mode, now, decide." %
          (a.title, slug, ", чат привязан" if a.session else ""))


def cmd_open(a):
    exact = slugify(a.query)
    if topic_dir(exact)[0]:
        slug = exact
    else:
        r = ranked(a.query)
        if not r or r[0][0] < 0.5:
            print("НЕ НАШЁЛ тему по «%s». Скажи пользователю прямо, не восстанавливай по догадке. Есть:" % a.query)
            for sc, s, arch, meta in r[:8]:
                print("  %s · %s%s" % (s, meta.get("title"), " (архив)" if arch else ""))
            sys.exit(4)
        top_words = set(words(r[0][1]))
        # «claudecodeui» и «claudecodeui-shared»: короткое имя целиком внутри длинного — не угадывать
        nested = [x for x in r[1:] if x[0] >= 0.5 and top_words and top_words <= set(words(x[1]))]
        if len(r) > 1 and r[1][0] >= 0.5 and (r[1][0] >= r[0][0] - 0.1 or nested):
            print("НЕСКОЛЬКО ПОХОЖИХ ТЕМ — один закрытый вопрос пользователю («Вы про X — так?»):")
            for sc, s, arch, meta in r[:4]:
                if sc >= 0.5:
                    print("  %s · %s · последнее обращение %s%s" % (
                        s, meta.get("title"), human(meta.get("last_touched")), " (архив)" if arch else ""))
            sys.exit(4)
        slug = r[0][1]
    was_archived = topic_dir(slug)[1]
    prev = load_meta(topic_dir(slug)[0]).get("last_touched")
    d, meta = bind(slug, a.session) if a.session else (restore(slug)[0], None)
    meta = meta or load_meta(d)
    me = label(a.session)
    print("ТЕМА: «%s» (%s) · последнее обращение до этого: %s · остановились: %s%s" % (
        meta["title"], slug, human(prev), stopped_at(d),
        " · ВОЗВРАЩЕНА ИЗ АРХИВА" if was_archived else ""))
    print("Первой строкой ответа пользователю скажи, какую тему поднял и на чём остановились — ошибку он увидит сразу.\n")
    for name, title in (("mode.md", "РЕЖИМ РАБОТЫ — читай первым, так работаем в этой теме"),
                        ("now.md", "СЕЙЧАС"), ("decisions.md", "РЕШЕНИЯ")):
        print("===== %s =====\n%s\n" % (title, read(os.path.join(d, name)).strip()))
    print("Факты о состоянии (сервис работает, файл есть, «ждём X») записаны с датой — перед действием "
          "перепроверь командой. Решения прими как есть, по отклонённым не ходи по кругу.")
    for lab, s in (meta.get("sessions") or {}).items():
        tp = s.get("transcript")
        if lab == me or not tp or not os.path.exists(tp):
            continue
        base = parse(s.get("last_write") or s.get("bound") or "")
        mt = dt.datetime.fromtimestamp(os.path.getmtime(tp))
        st = load_state(s.get("session_id") or "")
        if "prompts" in st:
            behind = st.get("prompts", 0) - st.get("prompts_at_last_write", 0)
            stale, why = behind > 0, "пользователь написал ему %d сообщ. после его последней записи" % behind
        else:
            stale = bool(base) and (mt - base).total_seconds() > LAG_SECONDS
            why = "чат говорил больше %d мин после своей последней записи" % (LAG_SECONDS // 60)
        if stale and (now() - mt).days < ARCHIVE_AFTER_DAYS:
            print("ОТСТАЁТ: чат %s — %s (запись %s, последнее в чате %s). Решения могли не попасть на "
                  "страницу — дочитай хвост помощником и допиши: topic.py tail %s" % (lab, why, human(base), human(mt), lab))
    pend = pending_memory(slug)
    if pend:
        print("В ДОЛГУЮ ПАМЯТЬ ждут переноса %d выводов: topic.py memory-pending" % len(pend))
    if meta.get("misses", 0) >= 2:
        print("ФОРМАТ ПОДВОДИТ: в этой теме пользователь %d раза говорил «ты забыл, что мы решили». "
              "Разбери, чего не хватило странице, и поправь формат (agent.md)." % meta["misses"])


def write_block(a, name, cap, header):
    slug = current_topic(a)
    text = sys.stdin.read().strip()
    if not text:
        die("Пустой текст — ничего не записано", 2)
    check_secret(text)
    with locked(slug):
        d, _ = restore(slug)
        meta = load_meta(d)
        body = header(meta) + text + "\n"
        if len(body) > cap:
            die("ОТКАЗ: %s выйдет %d знаков при потолке %d. Ужми; вытесненное — topic.py spill." % (
                name, len(body), cap), 3)
        write(os.path.join(d, name), body)
        touch(d, meta)
    mark_written(a.session)
    print("ок: %s %d/%d знаков" % (name, len(body), cap))


def cmd_mode(a):
    write_block(a, "mode.md", MODE_MAX, lambda m: "# Режим работы — %s\n_обновлено %s_\n\n" % (m["title"], human(ts())))


def cmd_now(a):
    if not a.session:
        die("--session обязателен: у каждого чата свой раздел", 2)
    slug = current_topic(a)
    text = sys.stdin.read().strip()
    if not text:
        die("Пустой текст — ничего не записано", 2)
    check_secret(text)
    me = label(a.session)
    absorb = {x.strip() for x in (a.absorb or "").split(",") if x.strip()}
    with locked(slug):
        d, _ = restore(slug)
        meta = load_meta(d)
        secs = read_sections(d)
        live = []
        for l in absorb:
            tp = ((meta.get("sessions") or {}).get(l) or {}).get("transcript")
            if l != me and tp and os.path.exists(tp) and not os.environ.get("TOPICS_FORCE_ABSORB") and \
                    (now() - dt.datetime.fromtimestamp(os.path.getmtime(tp))).total_seconds() < LIVE_HOURS * 3600:
                live.append(l)
        if live:
            die("ОТКАЗ: чат %s говорил меньше %d ч назад — он, возможно, ещё работает, его раздел не трогай. "
                "Пиши только свой раздел; не влезает — ужми свой." % (", ".join(live), LIVE_HOURS), 6)
        gone = [(l, b) for l, b in secs if l in absorb and l != me]
        keep = [(l, b) for l, b in secs if l not in absorb and l != me]
        keep.append((me, "## Чат %s · %s\n%s\n" % (me, human(ts()), text)))
        new = render_now(meta["title"], keep)
        if len(new) > NOW_MAX:
            sizes = ", ".join("%s: %d" % (l, len(b)) for l, b in keep)
            die("ОТКАЗ: «Сейчас» выйдет %d знаков при потолке %d (разделы — %s). Ужми свой текст; "
                "вытесненное сохрани через topic.py spill. Если ты уже свёл в свой раздел содержание "
                "разделов других чатов — добавь --absorb метка1,метка2 (их текст уйдёт в архив темы)."
                % (len(new), NOW_MAX, sizes), 3)
        for l, b in gone:
            with open(os.path.join(d, "spill.md"), "a", encoding="utf-8") as f:
                f.write("\n## %s · раздел чата %s, вобран чатом %s\n%s\n" % (human(ts()), l, me, b.strip()))
        write(os.path.join(d, "now.md"), new)
        s = meta.setdefault("sessions", {}).setdefault(me, {"session_id": a.session, "bound": ts()})
        s["last_write"] = ts()
        s["transcript"] = s.get("transcript") or find_transcript(a.session)
        touch(d, meta)
    mark_written(a.session)
    print("ок: «Сейчас» %d/%d знаков%s" % (len(new), NOW_MAX, ", вобраны: " + ", ".join(l for l, _ in gone) if gone else ""))


def cmd_decide(a):
    slug = current_topic(a)
    check_secret(" ".join([a.what, a.why] + (a.rejected or [])))
    with locked(slug):
        d, _ = restore(slug)
        meta = load_meta(d)
        entry = "- %s · %s — потому что %s\n" % (now().strftime("%d.%m.%y"), a.what.strip(), a.why.strip())
        for r in a.rejected or []:
            entry += "  - отклонено: %s\n" % r.strip()
        with open(os.path.join(d, "decisions.md"), "a", encoding="utf-8") as f:
            f.write(entry)
        if a.glob:
            with open(TO_MEMORY, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": now().strftime("%y%m%d%H%M%S"), "topic": slug, "what": a.what,
                                    "why": a.why, "ts": ts(), "done": False}, ensure_ascii=False) + "\n")
        touch(d, meta)
    mark_written(a.session)
    size = decisions_size(slug)
    print("ок: решение записано%s. Решения %d/%d знаков%s" % (
        ", в очередь долгой памяти" if a.glob else "", size, DECISIONS_MAX,
        " — ПОТОЛОК ПРЕВЫШЕН: topic.py squeeze (сводка на stdin)" if size > DECISIONS_MAX else ""))


def cmd_squeeze(a):
    slug = current_topic(a)
    text = sys.stdin.read().strip()
    check_secret(text)
    with locked(slug):
        d, _ = restore(slug)
        meta = load_meta(d)
        body = "# Решения — %s\n_сжато %s, полный текст — decisions-archive.md_\n\n%s\n" % (meta["title"], human(ts()), text)
        if not text or len(body) > DECISIONS_MAX * 0.7:
            die("ОТКАЗ: сводка пустая или больше 70%% потолка (%d знаков) — сожми сильнее" % len(body), 3)
        with open(os.path.join(d, "decisions-archive.md"), "a", encoding="utf-8") as f:
            f.write("\n\n# Сохранено при сжатии %s\n\n%s" % (human(ts()), read(os.path.join(d, "decisions.md"))))
        write(os.path.join(d, "decisions.md"), body)
        touch(d, meta)
    mark_written(a.session)
    print("ок: решения сжаты до %d знаков, полный текст сохранён в архиве темы" % len(body))


def cmd_spill(a):
    slug = current_topic(a)
    text = sys.stdin.read().strip()
    check_secret(text)
    d, _ = restore(slug)
    with open(os.path.join(d, "spill.md"), "a", encoding="utf-8") as f:
        f.write("\n## %s · чат %s\n%s\n" % (human(ts()), label(a.session), text))
    print("ок: сохранено в архив темы (в чат не грузится)")


def cmd_tail(a):
    for slug, d, arch, meta in all_topics():
        s = (meta.get("sessions") or {}).get(a.label)
        if not s:
            continue
        tp = s.get("transcript")
        if not tp or not os.path.exists(tp):
            die("Переписка чата %s не найдена" % a.label, 2)
        since = s.get("last_write") or s.get("bound") or ""
        size = os.path.getsize(tp)
        out = []
        with open(tp, encoding="utf-8", errors="replace") as f:
            if size > 30_000_000:
                f.seek(size - 30_000_000)
                f.readline()
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") not in ("user", "assistant") or (o.get("timestamp") or "")[:19] <= since[:19]:
                    continue
                c = (o.get("message") or {}).get("content")
                t = c if isinstance(c, str) else " ".join(
                    x.get("text", "") for x in (c or []) if isinstance(x, dict) and x.get("type") == "text")
                if t.strip():
                    out.append("[%s] %s" % ("пользователь" if o["type"] == "user" else "Клод", t.strip()))
        text = "\n\n".join(out)
        print("Хвост чата %s после его записи %s (тема %s):\n\n%s" % (a.label, human(since), slug, text[-a.chars:]))
        return
    die("Чат %s ни к одной теме не привязан" % a.label, 2)


def cmd_list(a):
    act, arc = [], []
    for slug, d, arch, meta in all_topics():
        (arc if arch else act).append((meta.get("last_touched") or "", slug, d, meta))
    if not act and not arc:
        print("Тем пока нет.")
        return
    print("Открытые темы:")
    for lt, slug, d, meta in sorted(act, reverse=True):
        print("  %s · %s · %s · остановились: %s" % (slug, meta["title"], human(lt), stopped_at(d)))
    recent = [x for x in arc if (parse(x[3].get("archived_at") or "") or now()) > now() - dt.timedelta(days=RECENT_ARCHIVE_DAYS)]
    if recent or a.all:
        print("Недавно в архиве (поднимаются по упоминанию):" if not a.all else "Архив:")
        for lt, slug, d, meta in sorted(arc if a.all else recent, reverse=True):
            print("  %s · %s · последнее обращение %s" % (slug, meta["title"], human(lt)))


def cmd_find(a):
    for sc, slug, arch, meta in ranked(a.query)[:5]:
        print("%.2f  %s · %s%s" % (sc, slug, meta["title"], " (архив)" if arch else ""))


def cmd_skip(a):
    mark_written(a.session)
    print("ок: записывать нечего — отметил")


def cmd_oneoff(a):
    st = load_state(a.session)
    st.update({"session_id": a.session, "oneoff": True})
    save_state(a.session, st)
    print("ок: беседа разовая, тема не нужна")


def cmd_memory_pending(a):
    pend = pending_memory()
    if not pend:
        print("Очередь долгой памяти пуста.")
    for o in pend:
        print("%s · тема %s · %s — %s" % (o["id"], o["topic"], o["what"], o["why"]))
    if pend:
        print("Перенеси в ~/.claude/projects/-home-claude/memory/ по формату памяти, затем topic.py memory-done <id> …")


def cmd_memory_done(a):
    lines = []
    for line in read(TO_MEMORY).splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("id") in a.ids:
            o["done"] = True
        lines.append(json.dumps(o, ensure_ascii=False))
    write(TO_MEMORY, "\n".join(lines) + ("\n" if lines else ""))
    print("ок: отмечено перенесённым: %s" % ", ".join(a.ids))


def cmd_lifecycle(a):
    N = parse(a.now) if a.now else now()
    os.makedirs(ARCHIVE, exist_ok=True)
    log = []
    for slug, d, arch, meta in list(all_topics()):
        lt = parse(meta.get("last_touched") or "") or N
        if not arch:
            live = [lab for lab, s in (meta.get("sessions") or {}).items()
                    if s.get("transcript") and os.path.exists(s["transcript"])
                    and (N - dt.datetime.fromtimestamp(os.path.getmtime(s["transcript"]))).days < ARCHIVE_AFTER_DAYS]
            if (N - lt).days >= ARCHIVE_AFTER_DAYS and not live:
                log.append("в архив: %s (%d дн. без обращений)" % (slug, (N - lt).days))
                if not a.dry_run:
                    meta["archived_at"] = N.isoformat()
                    save_meta(d, meta)
                    shutil.move(d, os.path.join(ARCHIVE, slug))
            continue
        at = parse(meta.get("archived_at") or "") or lt
        if (N - at).days >= DELETE_AFTER_DAYS and (N - lt).days >= DELETE_AFTER_DAYS:
            pend = pending_memory(slug)
            if pend:
                log.append("НЕ удаляю %s: %d выводов не перенесены в долгую память" % (slug, len(pend)))
                continue
            log.append("удалено: %s (%d дн. в архиве, выводы перенесены)" % (slug, (N - at).days))
            if not a.dry_run:
                shutil.rmtree(d)
    print("%s lifecycle%s: %s" % (N.isoformat(), " (пробный)" if a.dry_run else "", "; ".join(log) or "без изменений"))


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--session", default="")
    common.add_argument("--topic", default="")
    p = argparse.ArgumentParser(description="Темы между чатами (agent.md рядом)")
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("new", parents=[common]); x.add_argument("slug"); x.add_argument("--title", required=True); x.add_argument("--aliases", default=""); x.set_defaults(f=cmd_new)
    x = sp.add_parser("open", parents=[common]); x.add_argument("query"); x.set_defaults(f=cmd_open)
    x = sp.add_parser("now", parents=[common]); x.add_argument("--absorb", default=""); x.set_defaults(f=cmd_now)
    x = sp.add_parser("mode", parents=[common]); x.set_defaults(f=cmd_mode)
    x = sp.add_parser("decide", parents=[common]); x.add_argument("--what", required=True); x.add_argument("--why", required=True)
    x.add_argument("--rejected", action="append"); x.add_argument("--global", dest="glob", action="store_true"); x.set_defaults(f=cmd_decide)
    x = sp.add_parser("squeeze", parents=[common]); x.set_defaults(f=cmd_squeeze)
    x = sp.add_parser("spill", parents=[common]); x.set_defaults(f=cmd_spill)
    x = sp.add_parser("tail", parents=[common]); x.add_argument("label"); x.add_argument("--chars", type=int, default=15000); x.set_defaults(f=cmd_tail)
    x = sp.add_parser("list", parents=[common]); x.add_argument("--all", action="store_true"); x.set_defaults(f=cmd_list)
    x = sp.add_parser("find", parents=[common]); x.add_argument("query"); x.set_defaults(f=cmd_find)
    x = sp.add_parser("skip", parents=[common]); x.set_defaults(f=cmd_skip)
    x = sp.add_parser("oneoff", parents=[common]); x.set_defaults(f=cmd_oneoff)
    x = sp.add_parser("memory-pending", parents=[common]); x.set_defaults(f=cmd_memory_pending)
    x = sp.add_parser("memory-done", parents=[common]); x.add_argument("ids", nargs="+"); x.set_defaults(f=cmd_memory_done)
    x = sp.add_parser("lifecycle", parents=[common]); x.add_argument("--dry-run", action="store_true"); x.add_argument("--now", default=""); x.set_defaults(f=cmd_lifecycle)
    a = p.parse_args()
    if a.cmd in ("skip", "oneoff") and not a.session:
        die("--session обязателен", 2)
    os.makedirs(ROOT, exist_ok=True)
    a.f(a)


if __name__ == "__main__":
    main()
