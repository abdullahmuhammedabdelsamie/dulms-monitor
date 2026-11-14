"""DULMS monitor: logs in, watches assignments + quizzes, alerts you on Telegram."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://dulms.deltauniv.edu.eg"
LOGIN_URL = f"{BASE}/login.aspx"
STATE_FILE = Path("state.json")
PAGES_FILE = Path("pages.json")
MAX_EVENTS = 8  # events per Telegram message
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------- basics
def env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"Missing secret: {name}")
    return value


def telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{env('TG_TOKEN')}/sendMessage",
        data={
            "chat_id": env("TG_CHAT_ID"),
            "text": text[:4000],
            "disable_web_page_preview": "true",
        },
        timeout=30,
    )
    r.raise_for_status()


def telegram_file(filename, content, caption=""):
    r = requests.post(
        f"https://api.telegram.org/bot{env('TG_TOKEN')}/sendDocument",
        data={"chat_id": env("TG_CHAT_ID"), "caption": caption[:900]},
        files={"document": (filename, content)},
        timeout=60,
    )
    r.raise_for_status()


def login(session):
    r = session.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # ASP.NET hidden fields (__VIEWSTATE, __EVENTVALIDATION, ...)
    data = {i["name"]: i.get("value", "") for i in soup.select("input[type=hidden][name]")}
    data.update(
        {
            "txtname": env("DULMS_USER"),
            "type": "1",  # 1 = Student
            "txtPass": env("DULMS_PASS"),
            "Button1": "Login",
        }
    )
    r = session.post(LOGIN_URL, data=data, timeout=30)
    r.raise_for_status()
    if "txtPass" in r.text:
        sys.exit("Login failed: wrong ID/password, or the site blocked this server.")


def save(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def first(x):
    return x[0] if isinstance(x, list) and x else x


# ------------------------------------------- assignments + quizzes (JSON)
def status_text(d, start, end, remaining):
    if d.get("IsStarted") == 0:
        return f"⏳ يفتح: {start}"
    if d.get("IsEnded") == 1:
        return f"🔒 اتقفل: {end}"
    return f"🟢 مفتوح، يقفل بعد: {remaining}"


def parse_assignments(data):
    items = {}
    for course in data:
        label = f"{first(course.get('CourseCode'))} - {course.get('CourseName')}"
        for d in course.get("Data") or []:
            if d.get("AssignmentId") is None:
                continue
            items[str(d["AssignmentId"])] = {
                "course": label,
                "name": d.get("AssignmentName"),
                "start": d.get("StartDate"),
                "end": d.get("EndDate"),
                "started": d.get("IsStarted"),
                "grade": d.get("Grade"),
                "max": d.get("MaximumGrade"),
                "status": status_text(d, d.get("StartDate"), d.get("EndDate"), d.get("RemainingTime")),
            }
    return items


def parse_quizzes(data):
    items = {}
    for d in data:
        if d.get("Quiz_Id") is None:
            continue
        items[str(d["Quiz_Id"])] = {
            "course": f"{d.get('Code')} - {d.get('Name')}",
            "name": d.get("QuizName"),
            "start": d.get("StartDate"),
            "end": d.get("EndDate"),
            "started": d.get("IsStarted"),
            "grade": d.get("Grade"),
            "max": d.get("MaxGrade"),
            "status": status_text(d, d.get("StartDate"), d.get("EndDate"), d.get("RemainingTime")),
        }
    return items


API_MONITORS = [
    {
        "key": "assignments",
        "label": "اسيمنت",
        "plural": "الاسيمنتات",
        "url": "/Assignment/GetStudentAssignments",
        "page": "/Assignment/AssignmentStudentList",
        "parse": parse_assignments,
    },
    {
        "key": "quizzes",
        "label": "كويز",
        "plural": "الكويزات",
        "url": "/Quizzes/GetStudentQuizzes",
        "page": "/Quizzes/StudentQuizzes",
        "parse": parse_quizzes,
    },
]

# only stable fields go in state.json (never the countdown), so it doesn't change every run
TRACKED = ("name", "start", "end", "started", "grade", "max")


def pick(item):
    return {k: item.get(k) for k in TRACKED}


def fmt_grade(it):
    g = it.get("grade")
    if g is None:
        return None
    if g == "":
        return "اتصحح"
    mx = it.get("max")
    return f"{g} / {mx}" if mx not in (None, "") else str(g)


def changes(old, new):
    out = []
    if not old.get("started") and new.get("started"):
        out.append("🟢 اتفتح دلوقتي")
    if new.get("grade") is not None and new.get("grade") != old.get("grade"):
        out.append(f"🎯 درجة جديدة: {fmt_grade(new)}")
    for field, label in (("start", "ميعاد الفتح"), ("end", "ميعاد القفل")):
        if old.get(field) and new.get(field) and old[field] != new[field]:
            out.append(f"📅 {label} اتغير: {old[field]} ← {new[field]}")
    return out


def fetch_json(session, mon):
    r = session.get(
        urljoin(BASE + "/", mon["url"]),
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": urljoin(BASE + "/", mon["page"]),
        },
        timeout=30,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("response is not JSON (session problem?)")
    if data == -1:
        raise RuntimeError("session died (-1)")
    return data


def check_api(session, state, mon):
    items = mon["parse"](fetch_json(session, mon))
    current = {i: pick(it) for i, it in items.items()}
    store = state.setdefault("api", {})

    if mon["key"] not in store:  # first time: save silently as baseline
        telegram(f"✅ بدأت مراقبة {mon['plural']} ({len(current)} حاليًا)")
        store[mon["key"]] = current
        return

    old_all = store[mon["key"]]
    events = []
    for i, it in items.items():
        old = old_all.get(i)
        head = f"{it['course']}\n{it['name']}"
        if old is None:
            events.append(f"🆕 {mon['label']} جديد\n{head}\n{it['status']}")
        else:
            ch = changes(old, current[i])
            if ch:
                events.append(f"{head}\n" + "\n".join(ch))

    if events:
        shown = events[:MAX_EVENTS]
        text = f"🔔 {mon['plural']}\n\n" + "\n\n".join(shown)
        if len(events) > MAX_EVENTS:
            text += f"\n\n... و{len(events) - MAX_EVENTS} كمان"
        text += "\n" + urljoin(BASE + "/", mon["page"])
        telegram(text)
    store[mon["key"]] = {**old_all, **current}


# ------------------------------------------------- optional generic pages
def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_items(html, selector, ignore=()):
    """Return {hash: text} for every table row on a page."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    items = {}
    for row in soup.select(selector):
        if row.find("tr"):
            continue
        cells = [clean(c.get_text(" ")) for c in row.find_all("td")]
        cells = [c for c in cells if c] or [clean(row.get_text(" "))]
        text = " | ".join(cells)
        for pattern in ignore:
            text = re.sub(pattern, "", text, flags=re.I)
        text = clean(text)
        if text:
            items[hashlib.sha1(text.encode("utf-8")).hexdigest()] = text
    return items


def check_page(session, state, index, page):
    name = page["name"]
    url = urljoin(BASE + "/", page["url"])
    r = session.get(url, timeout=30)
    r.raise_for_status()
    if "txtPass" in r.text:
        raise RuntimeError("redirected to login page")

    if page.get("dump") and name not in state.get("_dumped", []):
        raw = r.text.replace(env("DULMS_USER"), "USER_ID")
        telegram_file(f"page_{index}.html", raw.encode("utf-8"), f"{index}. {name}\n{url}")
        state.setdefault("_dumped", []).append(name)

    items = extract_items(r.text, page.get("selector", "tr"), page.get("ignore", []))
    if name not in state:
        state[name] = sorted(items)
        telegram(f"✅ بدأت مراقبة «{name}» ({len(items)} عنصر حاليًا)")
        return
    old = set(state[name])
    new = [t for h, t in items.items() if h not in old]
    if new:
        lines = [f"🔔 جديد في {name}:"] + [f"• {t[:300]}" for t in new[:MAX_EVENTS]]
        lines.append(url)
        telegram("\n".join(lines))
    state[name] = sorted(old | set(items))


# ------------------------------------------------------------------ main
def main():
    pages = json.loads(PAGES_FILE.read_text(encoding="utf-8")) if PAGES_FILE.exists() else []
    first_run = not STATE_FILE.exists()
    state = {} if first_run else json.loads(STATE_FILE.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers["User-Agent"] = UA
    login(session)
    if first_run:
        telegram("✅ تسجيل الدخول نجح وتليجرام شغال.")

    failed = False
    for mon in API_MONITORS:
        try:
            check_api(session, state, mon)
            save(state)
        except Exception as e:  # keep checking the rest
            failed = True
            print(f"ERROR on {mon['key']}: {e}")

    for index, page in enumerate(pages, 1):
        try:
            check_page(session, state, index, page)
            save(state)
        except Exception as e:
            failed = True
            print(f"ERROR on {page.get('name')}: {e}")

    # changes once a month -> one small commit, keeps GitHub's schedule alive
    state["_heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m")
    save(state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
