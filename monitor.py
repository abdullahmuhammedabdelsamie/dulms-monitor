"""DULMS monitor: logs in, watches assignments, quizzes, meetings, files and grades, alerts you on Telegram.
Also answers Telegram commands (/grades, /assignments, ...) and reminds you before deadlines."""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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


def activity_state(d):
    if d.get("IsStarted") == 0:
        return "upcoming"
    if d.get("IsEnded") == 1:
        return "closed"
    return "open"


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
                # extra fields, used only by reports/reminders (not saved in state)
                "submitted": d.get("AssignmentAnswerId") is not None,
                "late": d.get("IsLate") == 1,
                "grade": d.get("Grade"),
                "max": d.get("MaximumGrade"),
                "passed": d.get("IsPassed"),
                "remaining": d.get("RemainingTime"),
                "state": activity_state(d),
                "pending": d.get("AssignmentAnswerId") is None and activity_state(d) == "open",
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
            "attempts": d.get("AttemptsNo") or 0,
            "inprogress": bool(d.get("Inprogress")),
            "grade": d.get("Grade"),
            "max": d.get("MaxGrade"),
            "passed": d.get("IsPassed"),
            "remaining": d.get("RemainingTime"),
            "state": activity_state(d),
            "pending": activity_state(d) == "open"
            and ((d.get("AttemptsNo") or 0) == 0 or bool(d.get("Inprogress"))),
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
        return items

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
    return items


# ------------------------------------------- reports, reminders, Telegram commands
def telegram_long(text, limit=3800):
    chunk = ""
    for line in text.split("\n"):
        if chunk and len(chunk) + len(line) + 1 > limit:
            telegram(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        telegram(chunk)


def num(x):
    try:
        return f"{float(x):g}"
    except (TypeError, ValueError):
        return str(x)


def score(g, mx):
    if g is None:
        return "--"
    if g == "":
        return "اتصحح"
    return f"{num(g)} / {num(mx)}" if mx not in (None, "") else num(g)


def passed_mark(p):
    return " ✔ ناجح" if p == 1 else " ✖ راسب" if p == 0 else ""


def time_text(it):
    if it["state"] == "upcoming":
        return f"⏳ يفتح {it['start']}"
    if it["state"] == "closed":
        return "🔒 مقفول"
    return f"🟢 يقفل بعد {it['remaining']}"


def by_course(items):
    out = {}
    for it in items.values():
        out.setdefault(it["course"], []).append(it)
    return out


def report_assignments(items):
    if not items:
        return "📝 الاسيمنتات\n\nمفيش اسيمنتات متسجلة."
    lines = ["📝 حالة الاسيمنتات"]
    for course, its in by_course(items).items():
        lines += ["", f"📚 {course}"]
        for it in its:
            if it["submitted"]:
                parts = ["✅ اتسلّم" + (" (متأخر)" if it["late"] else "")]
                if it["grade"] is None:
                    parts.append("لسه ماتصححش")
                else:
                    parts.append(f"الدرجة: {score(it['grade'], it['max'])}{passed_mark(it['passed'])}")
            else:
                parts = ["❌ لسه ماتسلمش"]
            parts.append(time_text(it))
            lines.append(f"• {it['name']}\n   " + " | ".join(parts))
    return "\n".join(lines)


def report_quizzes(items):
    if not items:
        return "🧪 الكويزات\n\nمفيش كويزات متسجلة."
    lines = ["🧪 حالة الكويزات"]
    for course, its in by_course(items).items():
        lines += ["", f"📚 {course}"]
        for it in its:
            if it["inprogress"]:
                parts = ["⏳ محاولة شغالة"]
            elif it["attempts"] == 0:
                parts = ["❌ لسه ماتحلش"]
            else:
                parts = [f"✅ اتحل (محاولات: {it['attempts']})"]
            if it["grade"] is not None:
                parts.append(f"الدرجة: {score(it['grade'], it['max'])}{passed_mark(it['passed'])}")
            parts.append(time_text(it))
            lines.append(f"• {it['name']}\n   " + " | ".join(parts))
    return "\n".join(lines)


def report_grades(items):
    if not items:
        return "📊 الدرجات\n\nمفيش درجات متسجلة لسه."
    lines = ["📊 الدرجات"]
    for course, its in by_course(items).items():
        graded = [i for i in its if i.get("grade") is not None]
        lines += ["", f"📚 {course}"]
        for i in graded:
            extra = f" ({i['percent']})" if i.get("percent") not in (None, "") else ""
            state = f" - {i['status']}" if i.get("status") not in (None, "") else ""
            lines.append(f"• {i['name']} — {score(i['grade'], i['max'])}{extra}{state}")
        if len(its) > len(graded):
            lines.append(f"   (و{len(its) - len(graded)} لسه من غير درجة)")
    return "\n".join(lines)


def report_meetings(items):
    opened = [i for i in items.values() if i["state"] == "open"]
    upcoming = [i for i in items.values() if i["state"] == "upcoming"]
    closed = sum(1 for i in items.values() if i["state"] == "closed")
    lines = ["🎥 الميتنجات"]
    if opened:
        lines += ["", "🔴 مفتوح دلوقتي:"]
        lines += [f"• {i['course']} - {i['name']}\n   {i['status']}" for i in opened]
    if upcoming:
        lines += ["", "⏳ جاي:"]
        lines += [f"• {i['course']} - {i['name']}\n   {i['status']}" for i in upcoming]
    if not opened and not upcoming:
        lines += ["", "مفيش ميتنجات مفتوحة أو جاية."]
    if closed:
        lines += ["", f"(و{closed} ميتنج خلصوا)"]
    return "\n".join(lines)


def report_todo(fresh):
    todo_a = [i for i in fresh["assignments"].values() if i["pending"]]
    todo_q = [i for i in fresh["quizzes"].values() if i["pending"]]
    live = [i for i in fresh["meetings"].values() if i["state"] == "open"]
    soon = [i for i in fresh["meetings"].values() if i["state"] == "upcoming"][:3]
    lines = ["📌 المطلوب دلوقتي"]
    if todo_a:
        lines += ["", "📝 اسيمنتات مفتوحة وماتسلمتش:"]
        lines += [f"• {i['course']} - {i['name']}\n   {time_text(i)}" for i in todo_a]
    if todo_q:
        lines += ["", "🧪 كويزات مفتوحة وماتحلتش:"]
        lines += [f"• {i['course']} - {i['name']}\n   {time_text(i)}" for i in todo_q]
    if live:
        lines += ["", "🔴 ميتنجات مفتوحة دلوقتي:"]
        lines += [f"• {i['course']} - {i['name']}\n   {i['status']}" for i in live]
    if not (todo_a or todo_q or live):
        lines += ["", "✅ مفيش حاجة مطلوبة دلوقتي."]
    if soon:
        lines += ["", "⏳ ميتنجات جاية:"]
        lines += [f"• {i['course']} - {i['name']}\n   {i['status']}" for i in soon]
    return "\n".join(lines)


HELP_TEXT = (
    "🤖 الأوامر:\n"
    "/todo - المطلوب دلوقتي\n"
    "/assignments - حالة الاسيمنتات (اتسلّمت؟ اتصححت؟)\n"
    "/quizzes - حالة الكويزات\n"
    "/grades - كل الدرجات اللي نزلت\n"
    "/meetings - الميتنجات\n\n"
    "الرد بيوصل بعد نص دقيقة تقريبًا (وممكن أكتر لو جيت هاب اتأخر)."
)

BOT_COMMANDS = [
    {"command": "todo", "description": "المطلوب دلوقتي"},
    {"command": "assignments", "description": "حالة الاسيمنتات"},
    {"command": "quizzes", "description": "حالة الكويزات"},
    {"command": "grades", "description": "كل الدرجات"},
    {"command": "meetings", "description": "الميتنجات"},
    {"command": "help", "description": "قايمة الأوامر"},
]

ALIASES = {
    "grades": "grades", "درجات": "grades", "الدرجات": "grades",
    "assignments": "assignments", "اسيمنتات": "assignments", "الاسيمنتات": "assignments", "واجبات": "assignments",
    "quizzes": "quizzes", "كويزات": "quizzes", "الكويزات": "quizzes",
    "meetings": "meetings", "ميتنجات": "meetings", "الميتنجات": "meetings",
    "todo": "todo", "المطلوب": "todo", "مطلوب": "todo",
    "help": "help", "start": "help", "مساعدة": "help",
}

# command -> (data it needs, how to build the reply)
REPORTS = {
    "grades": (("grades",), lambda f: report_grades(f["grades"])),
    "assignments": (("assignments",), lambda f: report_assignments(f["assignments"])),
    "quizzes": (("quizzes",), lambda f: report_quizzes(f["quizzes"])),
    "meetings": (("meetings",), lambda f: report_meetings(f["meetings"])),
    "todo": (("assignments", "quizzes", "meetings"), report_todo),
}


def tg_updates(offset):
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(
        f"https://api.telegram.org/bot{env('TG_TOKEN')}/getUpdates", params=params, timeout=30
    )
    r.raise_for_status()
    return r.json().get("result", [])


def set_bot_commands():
    r = requests.post(
        f"https://api.telegram.org/bot{env('TG_TOKEN')}/setMyCommands",
        json={"commands": BOT_COMMANDS},
        timeout=30,
    )
    r.raise_for_status()


def webhook_active():
    r = requests.get(f"https://api.telegram.org/bot{env('TG_TOKEN')}/getWebhookInfo", timeout=30)
    r.raise_for_status()
    return bool(r.json().get("result", {}).get("url"))


def command_of(text):
    words = (text or "").strip().lstrip("/").split()
    return ALIASES.get(words[0].split("@")[0].lower()) if words else None


def handle_commands(state, fresh):
    if webhook_active():  # commands arrive through the relay (worker.js) and run on their own
        return
    if "_tg_offset" not in state:  # first time: ignore old chat history, show the menu
        old = tg_updates(None)
        state["_tg_offset"] = max((u["update_id"] for u in old), default=0) + 1
        try:
            set_bot_commands()
        except Exception as e:
            print(f"setMyCommands failed: {e}")
        telegram("✅ الأوامر اتفعلت.\n\n" + HELP_TEXT)
        return

    owner = env("TG_CHAT_ID")
    for u in tg_updates(state["_tg_offset"]):
        state["_tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != owner:  # only answer the owner
            save(state)
            continue
        cmd = command_of(msg.get("text"))
        if cmd is None:
            telegram("مش فاهم الأمر ده. اكتب /help.")
        elif cmd == "help":
            telegram(HELP_TEXT)
        else:
            needs, build = REPORTS[cmd]
            if all(k in fresh for k in needs):
                telegram_long(build(fresh))
            else:
                telegram("⚠️ ما قدرتش أحمّل البيانات من الموقع دلوقتي، جرّب كمان شوية.")
        save(state)


def run_command(text):
    """Triggered by the relay the moment you send a command on Telegram."""
    cmd = command_of(text)
    if cmd is None:
        telegram("مش فاهم الأمر ده. اكتب /help.")
        return
    if cmd == "help":
        telegram(HELP_TEXT)
        return
    needs, build = REPORTS[cmd]
    by_key = {m["key"]: m for m in API_MONITORS}
    try:
        session = requests.Session()
        session.headers["User-Agent"] = UA
        login(session)
        fresh = {k: by_key[k]["parse"](fetch_json(session, by_key[k])) for k in needs}
    except (Exception, SystemExit) as e:  # login() exits on a wrong password
        print(f"command failed: {e}")
        telegram("⚠️ ما قدرتش أحمّل البيانات من الموقع دلوقتي، جرّب كمان شوية.")
        sys.exit(1)
    telegram_long(build(fresh))


REMIND_HOURS = (24, 3)


def parse_remaining(text):
    """'2 days 3 hours' / '05:30:00' / Arabic -> timedelta, or None if the format is unknown."""
    if not text:
        return None
    t = str(text).lower()
    total, found = 0, False
    for pattern, secs in (
        (r"(\d+)\s*(?:days?|d\b|يوم|أيام|ايام)", 86400),
        (r"(\d+)\s*(?:hours?|hrs?|h\b|ساعة|ساعات|ساعه)", 3600),
        (r"(\d+)\s*(?:minutes?|mins?|m\b|دقيقة|دقائق|دقيقه)", 60),
    ):
        for m in re.finditer(pattern, t):
            total += int(m.group(1)) * secs
            found = True
    for word, secs in (("يومين", 172800), ("ساعتين", 7200), ("دقيقتين", 120)):
        if word in t:
            total += secs
            found = True
    if found:
        return timedelta(seconds=total)
    m = re.search(r"(\d+):(\d{2})(?::(\d{2}))?", t)
    if m:
        return timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3) or 0))
    return None


def check_reminders(state, fresh):
    sent_all = state.setdefault("reminders", {})
    seen = set()
    for key, label, todo in (("assignments", "اسيمنت", "ماتسلمش"), ("quizzes", "كويز", "ماتحلش")):
        for iid, it in (fresh.get(key) or {}).items():
            if not it.get("pending"):
                continue
            rid = f"{key}:{iid}"
            seen.add(rid)
            left = parse_remaining(it.get("remaining"))
            if left is None:
                continue
            hours = left.total_seconds() / 3600
            reached = [h for h in REMIND_HOURS if hours <= h]
            sent = sent_all.get(rid, [])
            if reached and any(h not in sent for h in reached):
                tight = "3 ساعات" if min(reached) == 3 else "24 ساعة"
                telegram(
                    f"⏰ باقي أقل من {tight} على قفل {label} و{todo}\n"
                    f"{it['course']}\n{it['name']}\n🟢 يقفل بعد: {it['remaining']}"
                )
                sent_all[rid] = sorted(set(sent) | set(reached))
    # forget finished items, but only for lists we actually loaded this run
    loaded = tuple(f"{k}:" for k in ("assignments", "quizzes") if k in fresh)
    for rid in list(sent_all):
        if rid.startswith(loaded) and rid not in seen:
            del sent_all[rid]


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
    command = os.environ.get("TG_TEXT", "").strip()
    if command:  # a command from Telegram: answer it and stop (state is not touched)
        run_command(command)
        return

    pages = json.loads(PAGES_FILE.read_text(encoding="utf-8")) if PAGES_FILE.exists() else []
    first_run = not STATE_FILE.exists()
    state = {} if first_run else json.loads(STATE_FILE.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers["User-Agent"] = UA
    login(session)
    if first_run:
        telegram("✅ تسجيل الدخول نجح وتليجرام شغال.")

    failed = False
    fresh = {}
    for mon in API_MONITORS:
        try:
            fresh[mon["key"]] = check_api(session, state, mon)
            save(state)
        except Exception as e:  # keep checking the rest
            failed = True
            print(f"ERROR on {mon['key']}: {e}")

    for step in (check_reminders, handle_commands):
        try:
            step(state, fresh)
            save(state)
        except Exception as e:
            failed = True
            print(f"ERROR in {step.__name__}: {e}")

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
