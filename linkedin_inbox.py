"""
Free LinkedIn sources (no SerpApi searches used):

A) LinkedIn JOB ALERT emails in your Gmail  -> new LinkedIn jobs
   Needs GitHub Secrets: GMAIL_ADDRESS, GMAIL_APP_PASSWORD  (Gmail app password, read-only use)

B) Posts you SHARE to your bot in Telegram (paste the post text and/or link)
   -> scored against your CV and saved to your Job Search channel.
"""
import email, html, imaplib, os, re
import requests

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
LINKEDIN_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
                    "jobs-listings@linkedin.com")
JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
ERRORS = []   # read by job_search.py -> Errors channel
SCAM_WORDS = ("paid recruitment", "registration fee", "registration charges", "service charge",
              "processing fee", "visa charges", "pay for visa", "recruitment fee", "placement fee")


# ------------------------------------------------------------------ A) Gmail job alerts
def _parts(msg):
    plain, htm = "", ""
    for part in msg.walk():
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            txt = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        except Exception:
            continue
        if ct == "text/plain":
            plain += txt + "\n"
        else:
            htm += txt
    return plain, htm


def parse_alert(msg):
    """Extract jobs (title, company, location, link) from one LinkedIn job-alert email."""
    plain, htm = _parts(msg)
    return parse_alert_text(plain, htm)


def parse_alert_text(plain, htm):
    jobs, ids = [], set()
    skip = ("view job", "http", "---", "apply", "see all", "promoted", "actively recruiting",
            "easy apply", "applicant", "connection", "alumni", "new", "be an early applicant")
    # LinkedIn alert emails: job blocks separated by "-----" lines.
    # Each block = Title / Company / Location / (optional extra lines) / "View job: <link>"
    for block in re.split(r"\n-{5,}\s*\n", plain):
        m = JOB_LINK.search(block)
        if not m or m.group(1) in ids:
            continue
        before = block[:m.start()]
        lines = [l.strip() for l in before.splitlines() if l.strip()]
        lines = [l for l in lines if not l.lower().startswith(("view job", "your job alert"))
                 and not re.search(r"new jobs? match|jobs? match your", l.lower())]
        if len(lines) < 2:
            continue
        ids.add(m.group(1))
        title, company = lines[0], lines[1]
        loc = lines[2] if len(lines) > 2 and not re.search(
            r"actively hiring|apply with|easy apply|applicant|connection|alumni|promoted|school", lines[2].lower()) else ""
        jobs.append(_job(m.group(1), title, company, loc))
    if not jobs and htm:
        for m in re.finditer(r'href="(https?://[^"]*linkedin\.com/(?:comm/)?jobs/view/(\d+)[^"]*)"[^>]*>(.*?)</a>',
                             htm, re.S):
            jid = m.group(2)
            text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", m.group(3))).split())
            if not text or jid in ids or text.lower().startswith(skip):
                continue
            ids.add(jid)
            after = html.unescape(re.sub(r"<[^>]+>", "\n", htm[m.end():m.end() + 2000]))
            after = [x.strip() for x in after.splitlines() if x.strip()
                     and not x.strip().lower().startswith(skip)]
            company = after[0] if after else ""
            loc = after[1] if len(after) > 1 else ""
            if " · " in company and not loc:
                company, loc = company.split(" · ", 1)
            jobs.append(_job(jid, text, company, loc))
    return jobs


def _job(jid, title, company, loc):
    if " · " in company and not loc:
        company, loc = company.split(" · ", 1)
    return {"title": title, "company_name": company, "location": loc,
            "apply_options": [{"title": "LinkedIn", "link": f"https://www.linkedin.com/jobs/view/{jid}/"}],
            "source": "linkedin_alert", "linkedin_id": jid}


def gmail_linkedin_jobs(days=3):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("Gmail: not set up (GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets missing) - skipping")
        return []
    jobs = []
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com")
        box.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        ok, _ = box.select('"[Gmail]/All Mail"', readonly=True)
        if ok != "OK":
            box.select("INBOX", readonly=True)
        senders = " OR ".join(LINKEDIN_SENDERS)
        typ, data = box.search(None, "X-GM-RAW", f'"from:({senders}) newer_than:{days}d"')
        nums = data[0].split() if typ == "OK" and data and data[0] else []
        print(f"Gmail: {len(nums)} LinkedIn alert email(s) in the last {days} days")
        for num in nums[-40:]:
            typ, raw = box.fetch(num, "(RFC822)")
            if typ == "OK" and raw and raw[0]:
                jobs += parse_alert(email.message_from_bytes(raw[0][1]))
        box.logout()
    except Exception as ex:
        print("  ! Gmail error:", ex)
        ERRORS.append(f"Gmail: {ex} (check GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets)")
    return jobs


# ------------------------------------------------------------------ B) posts shared to the bot
def bot_inbox(bot_token, inbox_chat_ids=(), output_chat_id=None, any_channel=True):
    """Posts you pasted into your INBOX channel (or sent to the bot privately).
    Returns (messages, last_update_id). The bot must be an admin of the inbox channel."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates",
                         params={"timeout": 0, "allowed_updates": '["message","channel_post"]'},
                         timeout=30).json()
    except Exception as ex:
        print("  ! Telegram inbox error:", ex)
        ERRORS.append(f"Telegram inbox: {ex}")
        return [], None
    inbox_ids = {str(i) for i in inbox_chat_ids if i}
    msgs, last = [], None
    for u in r.get("result", []):
        last = u["update_id"]
        m = u.get("channel_post") or u.get("message") or {}
        chat = m.get("chat") or {}
        is_inbox = str(chat.get("id")) in inbox_ids or (
            any_channel and chat.get("type") == "channel"
            and str(chat.get("id")) not in {str(x) for x in (output_chat_id if isinstance(output_chat_id, (list, tuple, set)) else [output_chat_id]) if x})
        raw = (m.get("text") or m.get("caption") or "").strip()
        if raw.lower().split("@")[0].split(" ")[0] in ("/search", "/run"):
            msgs.append({"update_id": u["update_id"], "chat_id": chat.get("id"),
                         "message_id": m.get("message_id"), "is_channel": chat.get("type") == "channel",
                         "text": raw, "command": "search", "chat_title": chat.get("title") or chat.get("first_name")})
            continue
        if not (is_inbox or chat.get("type") == "private"):
            if chat.get("type") == "channel":
                print(f"  (post in channel '{chat.get('title')}' id {chat.get('id')} ignored - "
                      f"put this id in config.json 'inbox_channel_ids' if it is your inbox)")
            continue
        text = m.get("text") or m.get("caption") or ""
        for ent in (m.get("entities") or []) + (m.get("caption_entities") or []):
            if ent.get("url"):
                text += "\n" + ent["url"]
        if is_inbox and chat.get("type") == "channel":
            print(f"  inbox post from channel '{chat.get('title')}' ({chat.get('id')})")
        if text.strip() and not text.strip().startswith("/start"):
            msgs.append({"update_id": u["update_id"], "chat_id": chat["id"],
                         "message_id": m.get("message_id"), "is_channel": chat.get("type") == "channel",
                         "text": text})
    return msgs, last


def ack_inbox(bot_token, last_update_id):
    """Tell Telegram we've handled everything up to last_update_id."""
    if last_update_id is not None:
        try:
            requests.get(f"https://api.telegram.org/bot{bot_token}/getUpdates",
                         params={"offset": last_update_id + 1, "timeout": 0}, timeout=30)
        except Exception:
            pass


def split_shared(text):
    links = re.findall(r"https?://\S+", text)
    li_links = [l.rstrip(").,") for l in links if "linkedin.com" in l or "lnkd.in" in l]
    body = re.sub(r"https?://\S+", " ", text).strip()
    first = next((l.strip(" 🚨📌🔹*#") for l in body.splitlines() if len(l.strip()) > 8), "LinkedIn post")
    scams = [w for w in SCAM_WORDS if w in body.lower()]
    return body, li_links, first[:90], scams
