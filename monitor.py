"""DULMS monitor: logs in, checks the pages in pages.json, sends new rows to Telegram."""
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
MAX_NEW = 15  # more new rows than this in one run = probably a changing timestamp
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_items(html, selector, ignore=()):
    """Return {hash: text} for every row on the page.

    `ignore` = regexes removed from each row before hashing (e.g. "2 hours ago").
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    items = {}
    for row in soup.select(selector):
        if row.find("tr"):  # skip wrapper tables
            continue
        cells = [clean(c.get_text(" ")) for c in row.find_all("td")]
        cells = [c for c in cells if c] or [clean(row.get_text(" "))]
        text = " | ".join(cells)
        for pattern in ignore:
            text = re.sub(pattern, "", text, flags=re.I)
        text = clean(text)
        if not text:
            continue
        items[hashlib.sha1(text.encode("utf-8")).hexdigest()] = text
    return items


def save(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    pages = json.loads(PAGES_FILE.read_text(encoding="utf-8"))
    first_run = not STATE_FILE.exists()
    state = {} if first_run else json.loads(STATE_FILE.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers["User-Agent"] = UA
    login(session)
    if first_run:
        telegram("✅ تسجيل الدخول نجح وتليجرام شغال.")

    failed = False
    for page in pages:
        name = page["name"]
        url = urljoin(BASE + "/", page["url"])
        if "REPLACE_ME" in url:
            print(f"skip {name}: URL not set yet")
            continue
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            if "txtPass" in r.text:
                raise RuntimeError("redirected to login page")
            items = extract_items(r.text, page.get("selector", "tr"), page.get("ignore", []))

            if name not in state:  # first time: save silently as baseline
                state[name] = sorted(items)
                msg = f"✅ بدأت مراقبة «{name}» ({len(items)} عنصر حاليًا)"
                if not items:
                    msg += "\n⚠️ مالقيتش أي عناصر، هنظبط الإعدادات."
                telegram(msg)
            else:
                old = set(state[name])
                new = [t for h, t in items.items() if h not in old]
                if len(new) > MAX_NEW:
                    lines = [
                        f"⚠️ «{name}»: {len(new)} عنصر اتغيروا مرة واحدة.",
                        "غالبًا في وقت أو عداد متغير جوه الصفحة. ابعتلي شكلها عشان أظبطها.",
                        "أمثلة:",
                    ]
                    lines += [f"• {t[:200]}" for t in new[:5]]
                    telegram("\n".join(lines))
                elif new:
                    lines = [f"🔔 جديد في {name}:"]
                    lines += [f"• {t[:300]}" for t in new]
                    lines.append(url)
                    telegram("\n".join(lines))
                state[name] = sorted(old | set(items))
            save(state)
        except Exception as e:  # keep checking the other pages
            failed = True
            print(f"ERROR on {name}: {e}")

    try:  # close the session politely
        session.get(urljoin(BASE + "/", "/Login/SignOut"), timeout=15)
    except Exception:
        pass

    # changes once a month -> one small commit, keeps GitHub's schedule alive
    state["_heartbeat"] = datetime.now(timezone.utc).strftime("%Y-%m")
    save(state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
