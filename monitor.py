"""DULMS monitor: logs in, watches assignments, quizzes, meetings, files and grades, alerts you on Telegram."""
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


# ------------------------------------------------ JSON monitors (the site's own data)
def status_text(d):
    if d.get("IsStarted") == 0:
        return f"⏳ يفتح: {d.get('StartDate')}"
    if d.get("IsEnded") == 1:
        return f"🔒 اتقفل: {d.get('EndDate')}"
    return f"🟢 مفتوح، يقفل بعد: {d.get('RemainingTime')}"


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
                "status": status_text(d),
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
            "status": status_text(d),
        }
    return items


def parse_files(data):
    items = {}
    for d in data:
        if d.get("FileId") is None:
            continue
        who = d.get("NameEn_Employee") or ""
        if d.get("NameEn_Title"):
            who = f"{d['NameEn_Title']} / {who}"
        items[str(d["FileId"])] = {
            "course": f"{d.get('Code')} - {d.get('Name')}",
            "name": d.get("FileName"),
            "where": f"أسبوع {d.get('WeekNumber')} / {d.get('NameEn')}",
            "by": who,
        }
    return items


def parse_grades(data):
    items = {}
    for course in data:
        label = f"{course.get('Code')} - {course.get('CourseName')}"
        for d in course.get("Data") or []:
            if d.get("QuizOrAssignId") is None:
                continue
            items[f"{d.get('TypeId')}:{d['QuizOrAssignId']}"] = {
                "course": label,
                "name": f"{d.get('Type')}: {d.get('QuizOrAssignName')}",
                "grade": d.get("StudentGrade"),
                "max": d.get("MaxGrade"),
                "percent": d.get("studentPercent"),
                "status": d.get("StudentStatus"),
            }
    return items


def meeting_state(d):
    """Same rules the site's own page uses: upcoming / open / closed."""
    if d.get("IsStarted") == 0 and d.get("CloseDate") is None:
        return "upcoming"
    if d.get("CloseDate") is not None or d.get("IsEnded") == 1:
        return "closed"
    return "open"


def meeting_status(d, state):
    if state == "upcoming":
        return f"⏳ يفتح: {d.get('StartDateS')}"
    if state == "closed":
        return f"🔒 اتقفل: {d.get('CloseDateS') or d.get('EndDateS')}"
    return f"🟢 مفتوح دلوقتي، يقفل بعد: {d.get('RemainingTime')}"


def parse_meetings(data):
    items = {}
    for course in data:
        cid = first(course.get("CourseId"))
        for d in course.get("Data") or []:
            if d.get("StartDate") is None:  # a week with no meeting
                continue
            key = "|".join(
                str(d.get(k)) for k in ("WeekId", "StartDate", "MeetingName", "CreatedBy", "VLcId")
            )
            state = meeting_state(d)
            items[f"{cid}|{key}"] = {
                "course": course.get("CourseName"),
                "name": d.get("MeetingName"),
                "week": d.get("WeekNumber"),
                "start": d.get("StartDateS"),
                "state": state,
                "status": meeting_status(d, state),
            }
    return items


def meeting_on_new(it):
    if it["state"] == "closed":  # an old meeting showing up already finished: not news
        return None
    head = "🔴 ميتنج مفتوح دلوقتي" if it["state"] == "open" else "🆕 ميتنج جديد"
    return f"{head}\n{it['course']}\n{it['name']} (أسبوع {it['week']})\n{it['status']}"


def meeting_on_change(it, old, new):
    if old.get("state") != "open" and new.get("state") == "open":
        return f"🔴 الميتنج فتح دلوقتي!\n{it['course']}\n{it['name']}\n{it['status']}"
    return None


def schedule_changes(old, new):
    out = []
    if not old.get("started") and new.get("started"):
        out.append("🟢 اتفتح دلوقتي")
    for field, label in (("start", "ميعاد الفتح"), ("end", "ميعاد القفل")):
        if old.get(field) and new.get(field) and old[field] != new[field]:
            out.append(f"📅 {label} اتغير: {old[field]} ← {new[field]}")
    return out


def schedule_events(label):
    def on_new(it):
        return f"🆕 {label} جديد\n{it['course']}\n{it['name']}\n{it['status']}"

    def on_change(it, old, new):
        lines = schedule_changes(old, new)
        return f"{it['course']}\n{it['name']}\n" + "\n".join(lines) if lines else None

    return on_new, on_change


def grade_text(it):
    g = it.get("grade")
    if g == "":
        return "اتصحح"
    mx = it.get("max")
    return f"{g} / {mx}" if mx not in (None, "") else str(g)


def grade_event(it, title):
    lines = [title, it["course"], it["name"], f"الدرجة: {grade_text(it)}"]
    if it.get("percent") not in (None, ""):
        lines.append(f"النسبة: {it['percent']}")
    if it.get("status") not in (None, ""):
        lines.append(f"الحالة: {it['status']}")
    return "\n".join(lines)


def grade_on_new(it):
    return grade_event(it, "🎯 درجة جديدة") if it.get("grade") is not None else None


def grade_on_change(it, old, new):
    if new.get("grade") is None or new.get("grade") == old.get("grade"):
        return None
    if old.get("grade") is None:
        return grade_event(it, "🎯 درجة جديدة")
    return grade_event(it, f"🎯 الدرجة اتغيرت (كانت {old.get('grade')})")


def file_on_new(it):
    lines = ["📎 ملف جديد", it["course"], it["where"], it["name"]]
    if it.get("by"):
        lines.append(f"بواسطة: {it['by']}")
    return "\n".join(lines)


a_new, a_change = schedule_events("اسيمنت")
q_new, q_change = schedule_events("كويز")

# "tracked" = the only fields saved in state.json (never countdowns/download counts),
# so the file doesn't change on every run.
API_MONITORS = [
    {
        "key": "assignments", "plural": "الاسيمنتات",
        "url": "/Assignment/GetStudentAssignments", "page": "/Assignment/AssignmentStudentList",
        "method": "GET", "parse": parse_assignments,
        "tracked": ("name", "start", "end", "started"),
        "on_new": a_new, "on_change": a_change,
    },
    {
        "key": "quizzes", "plural": "الكويزات",
        "url": "/Quizzes/GetStudentQuizzes", "page": "/Quizzes/StudentQuizzes",
        "method": "GET", "parse": parse_quizzes,
        "tracked": ("name", "start", "end", "started"),
        "on_new": q_new, "on_change": q_change,
    },
    {
        "key": "meetings", "plural": "الميتنجات",
        "url": "/StudentMeeting/GetStudentMeeting", "page": "/StudentMeeting/MeetingList",
        "method": "GET", "parse": parse_meetings,
        "tracked": ("name", "start", "state"),
        "on_new": meeting_on_new, "on_change": meeting_on_change,
        "summary": lambda items: f" منهم {sum(1 for i in items.values() if i['state'] == 'open')} مفتوح دلوقتي",
    },
    {
        "key": "files", "plural": "الملفات والمواد",
        "url": "/ELearning/GetStudentElearningCourses", "page": "/ELearning/StudentElearning",
        "method": "GET", "parse": parse_files,
        "tracked": ("name",),
        "on_new": file_on_new, "on_change": lambda it, old, new: None,
    },
    {
        "key": "grades", "plural": "الدرجات",
        "url": "/GradeBook/StudentAssignmentsAndQuizes", "page": "/GradeBook/StudentGradeBook",
        "method": "POST", "parse": parse_grades,
        "tracked": ("grade", "max"),
        "on_new": grade_on_new, "on_change": grade_on_change,
        "summary": lambda items: f" منهم {sum(1 for i in items.values() if i.get('grade') is not None)} فيهم درجة",
    },
]


def fetch_json(session, mon):
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": urljoin(BASE + "/", mon["page"]),
    }
    if mon["method"] == "POST":
        headers["Content-Type"] = "application/json; charset=utf-8"
    r = session.request(mon["method"], urljoin(BASE + "/", mon["url"]), headers=headers, timeout=30)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError("response is not JSON (session problem?)")
    if data == -1:
        raise RuntimeError("session died (-1)")
    if data is None:
        raise RuntimeError("empty response")
    return data


def check_api(session, state, mon):
    items = mon["parse"](fetch_json(session, mon))
    current = {i: {k: it.get(k) for k in mon["tracked"]} for i, it in items.items()}
    store = state.setdefault("api", {})

    if mon["key"] not in store:  # first time: save silently as baseline
        extra = mon["summary"](items) if "summary" in mon else ""
        telegram(f"✅ بدأت مراقبة {mon['plural']} ({len(current)} حاليًا){extra}")
        store[mon["key"]] = current
        return

    old_all = store[mon["key"]]
    events = []
    for i, it in items.items():
        old = old_all.get(i)
        event = mon["on_new"](it) if old is None else mon["on_change"](it, old, current[i])
        if event:
            events.append(event)

    if events:
        text = f"🔔 {mon['plural']}\n\n" + "\n\n".join(events[:MAX_EVENTS])
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
