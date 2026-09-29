"""
UAE Job Alerts -> Telegram
--------------------------
1. Google Jobs (SerpApi) search for each title in config.json - UAE only.
2. Public LinkedIn hiring posts found via Google (hashtags in config.json).
3. ATS-style score vs cv.txt - only jobs >= min_ats_score are sent.
4. Freshness: skips jobs older than max_job_age_days.
5. Active check: opens each apply link; drops jobs whose links are dead or
   say "no longer accepting applications" / "position filled" etc.
6. No duplicates: remembers every job, apply link, LinkedIn post and post
   text already sent (seen_jobs.json) - even if the same job shows up again
   on another site or with a slightly different title.
7. Public company info (website, phone, address, role emails) - cached in
   companies.json so each company costs only one lookup ever.

GitHub Secrets: SERPAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import hashlib, html, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests

import linkedin_inbox as lii
import email_alerts as ea
import telegram_channels as tgc

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
CV_FILE = BASE / "cv.txt"
SEEN_FILE = BASE / "seen_jobs.json"
COMPANY_FILE = BASE / "companies.json"
USAGE_FILE = BASE / "usage.json"
SKILLS_FILE = BASE / "skills.txt"

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9"}

ERRORS = []          # problems during this run -> Errors channel
SEARCHES = [0]       # SerpApi searches used this run


def has_word(text, words):
    """True if any word/phrase appears as a WHOLE word (so 'intern' does not match 'internal')."""
    t = (text or "").lower()
    for w in words:
        w = (w or "").lower().strip()
        if w and re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", t):
            return True
    return False


def report_error(msg):
    print("  ! " + msg)
    ERRORS.append(msg)


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ROLE_PREFIXES = ("hr", "careers", "career", "jobs", "job", "recruit", "recruitment",
                 "talent", "hiring", "info", "contact", "enquir", "inquir", "admin",
                 "people", "cv", "resume", "apply", "vacanc", "hello", "office")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", ".png", ".jpg", ".jpeg", ".gif",
                   ".webp", ".svg", "domain.com", "email.com", "yourname", "@2x")
CLOSED_PHRASES = ("no longer accepting applications", "no longer available",
                  "job has expired", "this job has expired", "job is closed",
                  "position has been filled", "position is filled", "vacancy has been filled",
                  "this job is no longer", "job posting has expired", "posting is closed",
                  "applications are closed", "this position is closed", "job not found",
                  "the job you are looking for", "no longer open")
JOB_BOARDS = ("linkedin.", "indeed.", "bayt.", "naukrigulf.", "naukri.", "glassdoor.", "gulftalent.",
              "monster", "foundit.", "dubizzle.", "jooble.", "talent.com", "ziprecruiter.",
              "google.", "jobleads.", "jobrapido.", "careerjet.", "whatjobs.", "simplyhired.",
              "adzuna.", "jobs.ae", "drjobpro.", "laimoon.", "gulfjobs", "michaelpage.",
              "hays.", "roberthalf.", "ae.jobsdb", "tanqeeb.", "wuzzuf.", "workable.com",
              "lever.co", "greenhouse.io", "smartrecruiters.", "myworkdayjobs.", "bamboohr.",
              "zohorecruit.", "recruitee.", "teamtailor.", "successfactors.", "oraclecloud.",
              "icims.", "taleo.", "jobvite.", "ashbyhq.", "breezy.hr", "personio.")
PHONE_RE = re.compile(r"(?:\+971|00971)[\s-]?\(?\d{1,2}\)?[\s-]?\d{3}[\s-]?\d{4}")
TITLE_NOISE = re.compile(r"\b(uae|dubai|abu dhabi|sharjah|ajman|remote|hybrid|urgent|urgently|"
                         r"hiring|immediate|joiner|joining|required|needed|wanted|vacancy|"
                         r"m/f|f/m|full time|full-time|contract|permanent|nationals?|uaen)\b")


# ---------------------------------------------------------------- helpers
def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def norm(text):
    return " " + re.sub(r"[^a-z0-9+#&]+", " ", (text or "").lower()) + " "


def h(text):
    return hashlib.sha1(text.encode()).hexdigest()[:16]


def clean_url(url):
    """Strip tracking parameters so the same link always looks the same."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url


def app_link(url):
    """LinkedIn links as clean https://www.linkedin.com/... so phones open them in the LinkedIn app
    (regional sub-domains like ae.linkedin.com and tracking parameters stop the app from opening)."""
    try:
        p = urlparse(url)
    except Exception:
        return url
    if p.netloc.lower().endswith("linkedin.com") or p.netloc.lower() == "lnkd.in":
        if p.netloc.lower() == "lnkd.in":
            return url
        return urlunparse(("https", "www.linkedin.com", p.path.rstrip("/") + "/", "", "", ""))
    return url


def company_norm(name):
    n = norm(name)
    n = re.sub(r"\b(llc|l l c|fze|fzco|fz llc|dmcc|ltd|limited|group|co|company|inc|plc|"
               r"pjsc|psc|est|establishment|the)\b", " ", n)
    return " ".join(n.split())


def title_norm(title):
    t = TITLE_NOISE.sub(" ", norm(title).replace("&", " and "))
    t = re.sub(r"\b(sr)\b", "senior", t)
    return " ".join(sorted(set(w for w in t.split() if len(w) > 1)))


def job_keys(job):
    """Several fingerprints - a job is a duplicate if ANY of them was seen before."""
    keys = {
        "j:" + h(f"{title_norm(job.get('title',''))}|{company_norm(job.get('company_name',''))}"),
        # legacy key from the first version (so earlier sends are still remembered)
        h(f"{job.get('title','')}|{job.get('company_name','')}|{job.get('location','')}".lower()),
    }
    for o in job.get("apply_options") or []:
        if o.get("link"):
            keys.add("u:" + h(clean_url(o["link"])))
    return keys


# ---------------------------------------------------------------- ATS score
def load_skills():
    skills = []
    for line in SKILLS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [v.strip() for v in line.split("|") if v.strip()]
        skills.append((parts[0], [p.lower() for p in parts]))
    return skills


def found(variants, ntext):
    return any(f" {norm(v).strip()} " in ntext for v in variants)


def ats_score(title, text, cv_norm, skills, target_titles):
    """70% skills overlap (of the skills the job asks for) + 30% title match."""
    jd = norm(f"{title} {text}")
    job_skills = [(label, v) for label, v in skills if found(v, jd)]
    have = [label for label, v in job_skills if found(v, cv_norm)]
    missing = [label for label, v in job_skills if label not in have]
    skill_pct = len(have) / len(job_skills) if len(job_skills) >= 3 else 0.6

    title_n = norm(title)
    title_pct = 0.0
    for t in target_titles:
        words = [w for w in norm(t).split() if len(w) > 2]
        if words:
            title_pct = max(title_pct, sum(f" {w} " in title_n for w in words) / len(words))
    return round(100 * (0.7 * skill_pct + 0.3 * title_pct)), have, missing


# ---------------------------------------------------------------- freshness & active check
def age_days(posted_at):
    """'3 hours ago' -> 0, '5 days ago' -> 5, '30+ days ago' -> 30, 'Sep 20, 2026' -> days since, unknown -> None."""
    if not posted_at:
        return None
    s = posted_at.lower()
    from datetime import datetime, date
    for f in ("%b %d, %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return (date.today() - datetime.strptime(posted_at.strip(), f).date()).days
        except ValueError:
            pass
    m = re.search(r"(\d+)\+?\s*(minute|hour|day|week|month)", s)
    if not m:
        return 0 if ("today" in s or "just" in s) else None
    n, unit = int(m.group(1)), m.group(2)
    return {"minute": 0, "hour": 0, "day": n, "week": n * 7, "month": n * 30}[unit]


def link_status(url):
    """('open'|'closed'|'unknown', page_text). 'unknown' = site blocks bots - we keep those."""
    try:
        r = requests.get(url, headers=UA, timeout=12, allow_redirects=True)
    except requests.RequestException:
        return "unknown", ""
    if r.status_code in (404, 410):
        return "closed", ""
    if r.status_code != 200:
        return "unknown", ""
    text = r.text[:400000]
    if any(p in text.lower() for p in CLOSED_PHRASES):
        return "closed", ""
    return "open", text


def check_active(job):
    """Returns (is_active, confirmed_open, live_links, emails_found_on_apply_pages)."""
    links = [o for o in (job.get("apply_options") or []) if o.get("link")][:4]
    if not links:
        return True, False, [], []
    live, confirmed, closed, emails = [], False, 0, []
    for o in links:
        st, page = link_status(o["link"])
        if st == "closed":
            closed += 1
            continue
        live.append(o)
        confirmed = confirmed or st == "open"
        emails += EMAIL_RE.findall(page)
    return (closed < len(links)), confirmed, live, clean_emails(emails, allow_personal=True)[:4]


# ---------------------------------------------------------------- SerpApi
def serpapi(params):
    params = {**params, "api_key": SERPAPI_KEY}
    SEARCHES[0] += 1
    try:
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
    except requests.RequestException as ex:
        report_error(f"SerpApi connection failed: {ex}")
        return {}
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in data:
        err = str(data.get("error", r.status_code))
        if "hasn't returned any results" in err or "no results" in err.lower():
            print(f"  (SerpApi: no results for {params.get('engine')})")
        else:
            report_error(f"SerpApi ({params.get('engine')}): {err}")
        return {}
    return data


def combined_query(titles):
    return " OR ".join(titles)


def search_jobs(query, location):
    return serpapi({"engine": "google_jobs", "q": query, "location": location,
                    "gl": "ae", "hl": "en"}).get("jobs_results", [])


def search_linkedin_posts(hashtags, keywords, days):
    tags = " OR ".join(f'"{t}"' for t in hashtags)
    kws = " OR ".join(f'"{k}"' for k in keywords)
    q = f'site:linkedin.com/posts ({tags}) ({kws})'
    from datetime import date, timedelta
    since = date.today() - timedelta(days=days)
    tbs = f"cdr:1,cd_min:{since:%m/%d/%Y},cd_max:{date.today():%m/%d/%Y}"  # exact date range
    return serpapi({"engine": "google", "q": q, "gl": "ae", "hl": "en",
                    "num": 20, "tbs": tbs}).get("organic_results", [])


# ---------------------------------------------------------------- company info
def clean_emails(emails, allow_personal=False):
    out = []
    for e in emails:
        e = e.strip(".").lower()
        if any(b in e for b in BAD_EMAIL_PARTS):
            continue
        if not allow_personal and not e.split("@")[0].startswith(ROLE_PREFIXES):
            continue
        if e not in out:
            out.append(e)
    return out


def emails_from_site(website):
    found_emails = []
    if not website:
        return found_emails
    for url in [website] + [urljoin(website, p) for p in ("/contact", "/contact-us", "/careers")]:
        try:
            r = requests.get(url, headers=UA, timeout=12)
            if r.ok and "text/html" in r.headers.get("content-type", ""):
                found_emails += EMAIL_RE.findall(r.text)
        except requests.RequestException:
            pass
        if len(clean_emails(found_emails)) >= 3:
            break
    return clean_emails(found_emails)[:4]


def own_site(job):
    """If an apply link is on the company's own site (not a job board), return its homepage."""
    for o in job.get("apply_options") or []:
        host = urlparse(o.get("link", "")).netloc.lower()
        if host and not any(b in host for b in JOB_BOARDS):
            return f"https://{host.split('careers.')[-1].split('jobs.')[-1]}"
    return ""


def free_company_info(website):
    """Emails + UAE phone numbers from the company website - costs no SerpApi search."""
    info = {"website": website, "phone": "", "address": "", "emails": [], "rating": ""}
    phones, emails = [], []
    for url in [website] + [urljoin(website, p) for p in ("/contact", "/contact-us", "/careers")]:
        try:
            r = requests.get(url, headers=UA, timeout=12)
            if r.ok and "text/html" in r.headers.get("content-type", ""):
                emails += EMAIL_RE.findall(r.text)
                phones += PHONE_RE.findall(r.text)
        except requests.RequestException:
            pass
    info["emails"] = clean_emails(emails)[:4]
    info["phone"] = phones[0] if phones else ""
    return info


def company_info(name, cache, job=None, allow_paid=True):
    key = company_norm(name)
    if key in cache:
        return cache[key]
    site = own_site(job or {})
    if site:
        info = free_company_info(site)
        cache[key] = info
        return info
    if not allow_paid:
        return None
    info = {"website": "", "phone": "", "address": "", "emails": [], "rating": ""}
    data = serpapi({"engine": "google_maps", "q": f"{name} UAE", "type": "search",
                    "hl": "en", "gl": "ae"})
    place = data.get("place_results") or (data.get("local_results") or [{}])[0]
    if place:
        info["website"] = place.get("website", "") or ""
        info["phone"] = place.get("phone", "") or ""
        info["address"] = place.get("address", "") or ""
        if place.get("rating"):
            info["rating"] = f"{place['rating']} ({place.get('reviews', 0)} reviews)"
    info["emails"] = emails_from_site(info["website"])
    cache[key] = info
    return info


# ---------------------------------------------------------------- telegram
YOUR_NAME = "Abhijith K Jayan"


def email_block(emails, title):
    e = html.escape
    if not emails:
        return []
    out = ["", "<b>📧 Email your CV to:</b>"] + [f"✉️ {e(m)}" for m in emails[:5]]
    out.append(f"<i>Subject: Application – {e(title)} – {e(YOUR_NAME)}</i>")
    return out


def fmt_job(job, score, have, missing, info, post_emails, links, confirmed):
    e = html.escape
    ext = job.get("detected_extensions", {}) or {}
    lines = [f"<b>{e(job.get('title', 'Untitled'))}</b>",
             f"🏢 {e(job.get('company_name', ''))}",
             f"📍 {e(job.get('location', ''))}"]
    meta = " · ".join(x for x in [ext.get("schedule_type", ""), ext.get("posted_at", ""),
                                  ext.get("salary", "")] if x)
    if meta:
        lines.append(f"🕒 {e(meta)}")
    lines.append("🟢 Apply link checked: open" if confirmed else "🟡 Active on Google Jobs")
    lines.append(f"🎯 <b>ATS match: {score}%</b>")
    if have:
        lines.append(f"✅ {e(', '.join(have[:8]))}")
    if missing:
        lines.append(f"⚠️ Missing: {e(', '.join(missing[:6]))}")
    if links:
        lines += ["", "<b>Apply links:</b>"]
        for o in links[:5]:
            label = o.get("title", "Apply")
            if "linkedin.com" in o["link"]:
                label = "LinkedIn (opens app)"
            lines.append(f'• <a href="{e(app_link(o["link"]), quote=True)}">{e(label)}</a>')
    elif job.get("share_link"):
        lines += ["", f'<a href="{e(job["share_link"], quote=True)}">View job</a>']

    emails = list(dict.fromkeys(post_emails + (info or {}).get("emails", [])))
    lines += email_block(emails, job.get("title", ""))
    if info and any([info.get("website"), info.get("phone"), info.get("address")]):
        lines += ["", "<b>Company info:</b>"]
        if info.get("website"):
            lines.append(f'🌐 <a href="{e(info["website"], quote=True)}">'
                         f'{e(urlparse(info["website"]).netloc or info["website"])}</a>')
        if info.get("phone"):
            lines.append(f"📞 {e(info['phone'])}")
        if info.get("address"):
            lines.append(f"🏠 {e(info['address'])}")
        if info.get("rating"):
            lines.append(f"⭐ {e(info['rating'])}")
    return "\n".join(lines)


def fmt_post(post, have, tags):
    e = html.escape
    title = post.get("title", "LinkedIn post")
    snippet = post.get("snippet", "")
    date = post.get("date", "")
    emails = clean_emails(EMAIL_RE.findall(f"{title} {snippet}"), allow_personal=True)
    lines = ["🔗 <b>LinkedIn hiring post</b>" + (f" · {e(date)}" if date else ""),
             f"<b>{e(title)}</b>",
             f"<i>{e(snippet[:500])}</i>"]
    if have:
        lines.append(f"🎯 Matched: {e(', '.join(have[:8]))}")
    lines += ["", f'👉 <a href="{e(app_link(post["link"]), quote=True)}">Open in LinkedIn app</a>']
    lines += email_block(emails, title.split("|")[0].split(" - ")[0].strip()[:80])
    return "\n".join(lines)


def send(text, chat=None):
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat or CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 30) + 1)
            continue
        if not r.ok:
            report_error(f"Telegram send to {chat or CHAT_ID} failed: {r.status_code} {r.text[:150]}")
        return r.ok
    return False


def uae_now():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) + timedelta(hours=4)


def send_summary(cfg, full, counts, stats, max_age):
    """Run report -> Summary channel; problems -> Errors channel."""
    errors_chat, summary_chat = cfg.get("errors_chat_id"), cfg.get("summary_chat_id")
    e = html.escape
    if ERRORS and errors_chat:
        body = "\n".join(f"• {e(x[:300])}" for x in ERRORS[:15])
        send(f"⚠️ <b>Errors in job bot run</b> ({uae_now():%d %b %Y, %H:%M} UAE)\n{body}", errors_chat)
    if summary_chat:
        total = sum(counts.values())
        if not full and total == 0 and not ERRORS:
            return          # nothing happened in a quick check - don't clutter the Summary channel
        lines = [f"📊 <b>{e(cfg.get('_run_label', 'Run'))}</b>",
                 f"🕘 {uae_now():%d %b %Y, %H:%M} UAE", ""]
        lines += [f"{k}: <b>{v}</b>" for k, v in counts.items() if full or v]
        if not full and total == 0:
            lines.append("Nothing new.")
        if full:
            lines += ["", f"🚫 Skipped - duplicates: {stats['dup']}, older than {max_age}d: {stats['old']}, "
                          f"closed: {stats['closed']}, excluded: {stats['excluded']}"]
        lines.append(f"🔎 SerpApi searches used: {SEARCHES[0]} ({cfg.get('_usage', '')})")
        lines.append(f"{'⚠️' if ERRORS else '✅'} Errors: {len(ERRORS)}" + (" (see Errors channel)" if ERRORS else ""))
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": summary_chat, "text": "\n".join(lines), "parse_mode": "HTML",
                                "disable_notification": (not full and total == 0)}, timeout=30)
        if not r.ok:
            print("  ! summary send failed:", r.text[:150])


def diagnose_telegram():
    print("\n=== Telegram check ===")
    print(f"TELEGRAM_CHAT_ID currently set to: {CHAT_ID!r}")
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=20).json()
        print("Bot:", "@" + me.get("result", {}).get("username", "?") if me.get("ok") else me)
        ups = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=20).json()
        chats = {}
        for u in ups.get("result", []):
            for key in ("channel_post", "message", "my_chat_member", "chat_member"):
                if key in u:
                    c = u[key]["chat"]
                    chats[c["id"]] = f'{c.get("title") or c.get("first_name")} ({c["type"]})'
        if chats:
            print("Chats this bot can see (use the channel's number as TELEGRAM_CHAT_ID):")
            for cid, name in chats.items():
                print(f"   {cid}  ->  {name}")
        else:
            print("The bot sees no chats. Add it as ADMIN in the channel, post a message, run again.")
    except Exception as ex:
        print("Could not check Telegram:", ex)


# ---------------------------------------------------------------- main
def main():
    missing = [n for n, v in [("SERPAPI_KEY", SERPAPI_KEY), ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
                              ("TELEGRAM_CHAT_ID", CHAT_ID)] if not v]
    if missing:
        sys.exit("Missing secrets: " + ", ".join(missing))

    cfg = load_json(CONFIG, {})
    cv_norm = norm(CV_FILE.read_text(encoding="utf-8")) if CV_FILE.exists() else ""
    if len(cv_norm) < 200:
        sys.exit("cv.txt is empty - paste your CV text into it.")
    skills = load_skills()
    titles = cfg.get("job_titles", [])
    min_score = cfg.get("min_ats_score", 70)
    max_age = cfg.get("max_job_age_days", 14)
    exclude = [w.lower() for w in cfg.get("exclude_words", [])]
    seen = set(load_json(SEEN_FILE, []))
    companies = load_json(COMPANY_FILE, {})
    run_keys = set()          # duplicates inside this same run
    sent = 0
    stats = {"old": 0, "dup": 0, "low": 0, "closed": 0, "excluded": 0}
    all_chat = str(os.environ.get("ALL_JOBS_CHAT_ID") or cfg.get("all_jobs_chat_id") or "").strip()
    all_chat = all_chat if all_chat and all_chat != str(CHAT_ID) else ""
    print("All-jobs channel:", all_chat or "(not set - everything goes to Job Search)")
    # full search only at the daily 9 AM run (or manual run); other runs only check Gmail + bot inbox
    inbox, last_update = lii.bot_inbox(BOT_TOKEN, cfg.get("inbox_channel_ids", []), [CHAT_ID, all_chat],
                                       cfg.get("inbox_any_channel", True))
    commands = [m for m in inbox if m.get("command") == "search"]
    inbox = [m for m in inbox if not m.get("command")]
    scheduled_full = os.environ.get("RUN_SCHEDULE", "") in ("", cfg.get("daily_cron", "50 4 * * *"))
    full = scheduled_full or bool(commands)
    run_label = ("Search on request (/search)" if commands and not scheduled_full
                 else "Daily full search" if full else "Quick check (emails, Telegram channels, pasted posts)")
    print("Run type:", run_label)
    for c in commands:
        try:
            payload = {"chat_id": c["chat_id"], "text": "🔎 Search started… results will appear in your channels shortly."}
            if c.get("message_id"):
                payload["reply_parameters"] = {"message_id": c["message_id"], "allow_sending_without_reply": True}
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=20)
        except Exception:
            pass
    usage = load_json(USAGE_FILE, {})
    month = uae_now().strftime("%Y-%m")
    if usage.get("month") != month:
        usage = {"month": month, "searches": 0}
    budget = cfg.get("serpapi_monthly_budget", 240)
    if full and usage["searches"] >= budget:
        report_error(f"SerpApi monthly budget reached ({usage['searches']}/{budget}) - skipping paid searches "
                     f"until next month. Gmail alerts and pasted posts still work.")
        full = False

    # ---------- 1. Google Jobs
    matches, others = [], []
    queries = ([combined_query(titles)] if cfg.get("combine_job_searches", True) else titles) if full else []
    for query in queries:
        for loc in cfg.get("locations", ["United Arab Emirates"]):
            print(f"Searching: {query} | {loc}")
            for job in search_jobs(query, loc):
                keys = job_keys(job)
                if keys & (seen | run_keys):
                    stats["dup"] += 1
                    continue
                run_keys |= keys
                text = f"{job.get('title','')} {job.get('description','')}".lower()
                if has_word(text, exclude):
                    stats["excluded"] += 1
                    continue
                age = age_days((job.get("detected_extensions") or {}).get("posted_at", ""))
                if age is not None and age > max_age:
                    stats["old"] += 1
                    continue
                hl = " ".join(i for x in job.get("job_highlights", []) for i in x.get("items", []))
                score, have, miss = ats_score(job.get("title", ""),
                                              f"{job.get('description','')} {hl}",
                                              cv_norm, skills, titles)
                print(f"  {score:3d}%  {job.get('title')} - {job.get('company_name')}")
                if score >= min_score:
                    matches.append((score, job, have, miss, keys))
                else:
                    stats["low"] += 1
                    others.append((score, job, have, miss, keys))

    matches.sort(key=lambda m: m[0], reverse=True)
    lookups = 0
    for score, job, have, miss, keys in matches[: cfg.get("max_jobs_per_run", 15)]:
        active, confirmed, live, page_emails = check_active(job)
        if not active:
            print(f"  x closed: {job.get('title')} - {job.get('company_name')}")
            stats["closed"] += 1
            seen |= keys
            continue
        info = None
        name = job.get("company_name", "")
        if name:
            paid_ok = lookups < cfg.get("max_company_lookups_per_run", 1)
            before = len(companies)
            info = company_info(name, companies, job, allow_paid=paid_ok)
            if len(companies) > before and not own_site(job):
                lookups += 1
        post_emails = clean_emails(EMAIL_RE.findall(job.get("description", "")), allow_personal=True)
        post_emails = list(dict.fromkeys(post_emails + page_emails))
        msg = fmt_job(job, score, have, miss, info, post_emails, live, confirmed)
        if send(msg):
            sent += 1
            seen |= keys
            if all_chat:
                time.sleep(2)
                send(msg, all_chat)
        time.sleep(3)

    # ---------- 1b. every other job found -> all-jobs channel (with its ATS %)
    all_sent = 0
    if all_chat:
        others.sort(key=lambda m: m[0], reverse=True)
        for score, job, have, miss, keys in others[: cfg.get("max_all_jobs_per_run", 25)]:
            active, confirmed, live, page_emails = check_active(job)
            if not active:
                stats["closed"] += 1
                seen |= keys
                continue
            info = company_info(job.get("company_name", ""), companies, job, allow_paid=False) \
                if job.get("company_name") else None
            post_emails = clean_emails(EMAIL_RE.findall(job.get("description", "")), allow_personal=True)
            post_emails = list(dict.fromkeys(post_emails + page_emails))
            if send(fmt_job(job, score, have, miss, info, post_emails, live, confirmed), all_chat):
                all_sent += 1
                seen |= keys
            time.sleep(3)
        print(f"All-jobs channel: {all_sent} other job(s) sent")

    # ---------- 2. LinkedIn hiring posts (public, via Google)
    li = cfg.get("linkedin", {})
    posts_sent = 0
    if full and li.get("enabled") and (li.get("hashtags") or li.get("hashtag_groups")):
        min_hits = li.get("min_keyword_matches", 2)
        groups = li.get("searches", [li.get("keywords", [])])
        if li.get("combine_searches", True):
            groups = [[k for g in groups for k in g]]
        tag_groups = li.get("hashtag_groups") or [li.get("hashtags", [])]
        for tags, group in [(t, g) for t in tag_groups for g in groups]:
            print(f"LinkedIn posts: {tags} + {group}")
            for post in search_linkedin_posts(tags, group, li.get("max_age_days", 7)):
                link = post.get("link", "")
                if "linkedin.com" not in link:
                    continue
                age = age_days(post.get("date", ""))
                if age is not None and age > li.get("max_age_days", 15):
                    stats["old"] += 1
                    continue
                body = f"{post.get('title','')} {post.get('snippet','')}"
                keys = {"p:" + h(clean_url(link)),
                        "t:" + h(" ".join(norm(post.get("snippet", "")).split()[:30]))}
                if keys & (seen | run_keys):
                    stats["dup"] += 1
                    continue
                run_keys |= keys
                low = body.lower()
                if has_word(low, exclude + li.get("exclude_phrases", [])):
                    stats["excluded"] += 1
                    continue
                if li.get("require_any") and not has_word(low, li["require_any"]):
                    stats["low"] += 1   # not a hiring post (e.g. someone looking for a job)
                    continue
                if li.get("require_location_any") and not has_word(low, li["require_location_any"]):
                    stats["excluded"] += 1   # not a UAE job
                    continue
                nb = norm(body)
                have = [label for label, v in skills if found(v, nb) and found(v, cv_norm)]
                title_hit = any(all(f" {w} " in nb for w in norm(t).split()) for t in titles)
                if len(have) + (2 if title_hit else 0) < min_hits:
                    stats["low"] += 1
                    continue
                if posts_sent >= li.get("max_posts_per_run", 10):
                    break
                if send(fmt_post(post, have, []), all_chat or None):
                    posts_sent += 1
                    seen |= keys
                time.sleep(3)

    # ---------- 3. Job-alert EMAILS from all portals (Gmail) - free
    alerts_sent = 0
    uae_words = cfg.get("linkedin", {}).get("require_location_any", ["uae", "dubai", "abu dhabi", "sharjah"])
    email_jobs, per_source = ea.gmail_alert_jobs(cfg.get("email_sources"), cfg.get("gmail_days", 3))
    sent_by_source = {}
    for job in email_jobs:
        if not job.get("title"):
            continue
        keys = job_keys(job) | {"e:" + h(job["uid"])}
        if keys & (seen | run_keys):
            stats["dup"] += 1
            continue
        run_keys |= keys
        if has_word(f"{job['title']} {job['company_name']}", exclude + cfg.get("alert_exclude_words", [])):
            stats["excluded"] += 1
            continue
        tl = f" {job['title'].lower()} "
        if cfg.get("alert_title_keywords") and not any(k in tl for k in cfg["alert_title_keywords"]):
            stats["low"] += 1   # not a finance role
            continue
        if job["location"] and not has_word(job["location"], uae_words):
            stats["excluded"] += 1   # outside the UAE (e.g. Indeed India)
            continue
        age = age_days(job.get("posted", ""))
        if age is not None and age > max_age:
            stats["old"] += 1
            continue
        e = html.escape
        lines = [f"💼 <b>{e(job['source'])} job alert</b>", f"<b>{e(job['title'])}</b>"]
        if job["company_name"]:
            lines.append(f"🏢 {e(job['company_name'])}")
        if job["location"]:
            lines.append(f"📍 {e(job['location'])}")
        if job.get("posted"):
            lines.append(f"🕒 {e(job['posted'])}")
        if len(job.get("snippet", "")) >= 80:
            score, have, miss = ats_score(job["title"], job["snippet"], cv_norm, skills, titles)
            lines.append(f"🎯 Match (from summary): {score}%")
            if have:
                lines.append(f"✅ {e(', '.join(have[:6]))}")
        if job["apply_options"]:
            link = job["apply_options"][0]["link"]
            label = "Open in LinkedIn app" if "linkedin.com" in link else f"Open on {job['source']}"
            lines += ["", f'👉 <a href="{e(app_link(link), quote=True)}">{e(label)}</a>']
        if send("\n".join(lines), all_chat or None):
            alerts_sent += 1
            sent_by_source[job["source"]] = sent_by_source.get(job["source"], 0) + 1
            seen |= keys
        time.sleep(3)
    if sent_by_source:
        print("Email alerts sent by source:", sent_by_source)

    # ---------- 5. Public Telegram job channels (t.me/s/...) - free
    tg_sent = 0
    chans = [tgc.channel_name(c) for c in cfg.get("telegram_channels", [])]
    skipped_private = [c for c, n in zip(cfg.get("telegram_channels", []), chans) if not n]
    if skipped_private:
        print("Private/invalid channel links skipped (forward their posts to your inbox instead):", skipped_private)
    for name in [c for c in chans if c]:
        for post in tgc.fetch_channel(name, cfg.get("telegram_posts_per_channel", 20)):
            if tg_sent >= cfg.get("max_channel_posts_per_run", 30):
                break
            body = post["text"]
            words = " ".join(norm(body).split()[:40])
            keys = {"tg:" + post["id"], "s:" + h(words)}
            if keys & (seen | run_keys) or len(body) < 40:
                stats["dup"] += 1
                continue
            run_keys |= keys
            age = tgc.age_days_iso(post["date"])
            if age is not None and age > max_age:
                stats["old"] += 1
                seen |= keys
                continue
            low = body.lower()
            if has_word(low, exclude + li.get("exclude_phrases", [])) or \
                    any(w in low for w in lii.SCAM_WORDS):
                stats["excluded"] += 1
                seen |= keys
                continue
            if not has_word(low, uae_words):
                stats["excluded"] += 1          # not a UAE job
                seen |= keys
                continue
            if not any(k in f" {low} " for k in cfg.get("alert_title_keywords", [])):
                stats["low"] += 1               # not a finance / accounts / audit role
                seen |= keys
                continue
            first = next((l.strip(" 🚨📌🔹*#-•") for l in body.splitlines() if len(l.strip()) > 6), "Job post")[:90]
            score, have, miss = ats_score(first, body, cv_norm, skills, titles)
            e = html.escape
            lines = [f"📢 <b>From @{e(name)}</b>" + (f" · {age}d ago" if age else ""), f"<b>{e(first)}</b>",
                     f"<i>{e(body[:500])}{'…' if len(body) > 500 else ''}</i>",
                     f"🎯 <b>ATS match: {score}%</b>"]
            if have:
                lines.append(f"✅ {e(', '.join(have[:8]))}")
            if miss:
                lines.append(f"⚠️ Missing: {e(', '.join(miss[:6]))}")
            lines += email_block(clean_emails(EMAIL_RE.findall(body), allow_personal=True), first)
            lines += ["", f'👉 <a href="{e(post["link"], quote=True)}">Open post in Telegram</a>']
            msg = "\n".join(lines)
            ok = send(msg, all_chat or None)
            if ok and score >= min_score and all_chat:
                time.sleep(2)
                send(msg)
                sent += 1
            if ok:
                tg_sent += 1
                seen |= keys
            time.sleep(3)
    if chans:
        print(f"Telegram channels: {tg_sent} post(s) sent")

    # ---------- 4. Posts you shared to the bot in Telegram - free
    shared_sent = 0
    for m in inbox:
        body, li_links, first, scams = lii.split_shared(m["text"])
        key = "s:" + h(" ".join(norm(body).split()[:40]) or (li_links[0] if li_links else m["text"]))
        e = html.escape
        if key in seen:
            reply = "Already saved earlier ✅"
        else:
            score = None
            lines = ["📌 <b>Saved LinkedIn post</b>", f"<b>{e(first)}</b>"]
            if len(body) >= 150:
                score, have, miss = ats_score(first, body, cv_norm, skills, titles)
                lines.append(f"<i>{e(body[:450])}{'…' if len(body) > 450 else ''}</i>")
                lines.append(f"🎯 <b>ATS match: {score}%</b>")
                if have:
                    lines.append(f"✅ {e(', '.join(have[:8]))}")
                if miss:
                    lines.append(f"⚠️ Missing: {e(', '.join(miss[:6]))}")
            elif body:
                lines.append(f"<i>{e(body[:450])}</i>")
            if scams:
                lines.append(f"🚩 <b>Warning:</b> mentions {e(', '.join(scams))} - UAE law does not allow charging job seekers.")
            ems = clean_emails(EMAIL_RE.findall(body), allow_personal=True)
            lines += email_block(ems, first)
            for l in li_links[:2]:
                lines += ["", f'👉 <a href="{e(app_link(l), quote=True)}">Open in LinkedIn app</a>']
            text_out = "\n".join(lines)
            is_match = score is not None and score >= min_score
            if all_chat:
                ok = send(text_out, all_chat)
                if ok and is_match:
                    time.sleep(2)
                    send(text_out)
            else:
                ok = send(text_out)
            if ok:
                shared_sent += 1
                seen.add(key)
            where = "Job Search + all-jobs channel" if (is_match and all_chat) else ("all-jobs channel" if all_chat else "Job Search")
            sc = f" (ATS {score}%)" if score is not None else ""
            reply = f"Saved to {where}{sc} ✅" if ok else "Could not save - will retry next run"
        try:
            payload = {"chat_id": m["chat_id"], "text": reply, "disable_notification": True}
            if m.get("message_id"):
                payload["reply_parameters"] = {"message_id": m["message_id"], "allow_sending_without_reply": True}
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=20)
        except Exception:
            pass
        time.sleep(2)
    lii.ack_inbox(BOT_TOKEN, last_update)
    print(f"Email job alerts sent: {alerts_sent}, shared posts saved: {shared_sent}")

    total = sent + posts_sent + alerts_sent + shared_sent
    if full and total == 0 and cfg.get("notify_when_empty", True) and not (matches and sent == 0) \
            and not cfg.get("summary_chat_id"):
        send(f"No new UAE jobs (ATS ≥ {min_score}%) or matching LinkedIn posts today.")

    save_json(SEEN_FILE, sorted(seen)[-8000:])
    save_json(COMPANY_FILE, companies)
    ERRORS.extend(getattr(lii, "ERRORS", []))
    ERRORS.extend(getattr(ea, "ERRORS", []))
    ERRORS.extend(getattr(tgc, "ERRORS", []))
    usage["searches"] = usage.get("searches", 0) + SEARCHES[0]
    save_json(USAGE_FILE, usage)
    cfg["_run_label"] = run_label
    cfg["_usage"] = f"{usage['searches']}/{cfg.get('serpapi_monthly_budget', 240)} this month"
    send_summary(cfg, full, {
        "🎯 ATS matches → Job Search": sent,
        "📋 Other jobs → All jobs": locals().get("all_sent", 0),
        "💼 Email job alerts" + (" (" + ", ".join(f"{k} {v}" for k, v in sent_by_source.items()) + ")"
                                if sent_by_source else ""): alerts_sent,
        "🔗 LinkedIn hashtag posts": posts_sent,
        "📢 Telegram job channels": locals().get("tg_sent", 0),
        "📌 Your pasted posts": shared_sent,
    }, stats, max_age)
    print(f"\nSent {sent} job(s) + {posts_sent} LinkedIn post(s). Company lookups: {lookups}")
    print(f"Skipped - duplicates: {stats['dup']}, older than {max_age} days: {stats['old']}, "
          f"closed links: {stats['closed']}, low score: {stats['low']}, excluded: {stats['excluded']}")
    if matches and sent == 0 and posts_sent == 0 and stats["closed"] < len(matches):
        diagnose_telegram()
        sys.exit("Telegram delivery failed - see the chat list above.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as ex:
        if ex.code not in (None, 0):
            chat = load_json(CONFIG, {}).get("errors_chat_id")
            if chat and BOT_TOKEN:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                              json={"chat_id": chat, "text": f"⚠️ Job bot stopped: {ex.code}"}, timeout=30)
        raise
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        chat = load_json(CONFIG, {}).get("errors_chat_id")
        if chat and BOT_TOKEN:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": chat, "text": "⚠️ Job bot crashed:\n" + tb[-3500:]}, timeout=30)
        raise
