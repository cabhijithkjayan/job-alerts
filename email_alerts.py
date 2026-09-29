"""
Job-alert EMAILS from job portals -> jobs (free, read from Gmail).

Supported (format checked against real emails):
  LinkedIn, Indeed, Naukrigulf, GulfTalent
Generic reader (refined once their first emails arrive):
  Bayt, Michael Page, Charterhouse, Tiger Recruitment, Dubai Vacancy, Dubai Careers, others

SAFETY: many alert emails contain AUTO-LOGIN links (tokens that open your account).
Every link is rebuilt as a clean public job page - tokens are never forwarded.
"""
import email, html, imaplib, os, re
from urllib.parse import unquote, urlparse, quote

import linkedin_inbox as lii

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
ERRORS = []

DEFAULT_SOURCES = [
    {"name": "LinkedIn", "from": ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
                                  "jobs-listings@linkedin.com"], "parser": "linkedin"},
    {"name": "Indeed", "from": ["jobalert.indeed.com"], "parser": "indeed"},
    {"name": "Naukrigulf", "from": ["myalerts@naukrigulf.com", "recommendedjobs@naukrigulf.com",
                                    "jobalerts@naukrigulf.com"], "parser": "naukrigulf"},
    {"name": "GulfTalent", "from": ["jobs@gulftalent.com"], "parser": "gulftalent"},
    {"name": "Bayt", "from": ["bayt.com"], "parser": "generic", "domain": "bayt.com"},
    {"name": "Michael Page", "from": ["michaelpage"], "parser": "generic", "domain": "michaelpage"},
    {"name": "Charterhouse", "from": ["charterhouse"], "parser": "generic", "domain": "charterhouse"},
    {"name": "Tiger Recruitment", "from": ["tiger-recruitment", "tigerrecruitment"], "parser": "generic",
     "domain": "tiger"},
    {"name": "Dubai Vacancy", "from": ["dubaivacancy"], "parser": "generic", "domain": "dubaivacancy"},
    {"name": "Dubai Careers", "from": ["dubaicareers"], "parser": "generic", "domain": "dubaicareers"},
]
SECRET_MARKERS = ("jwt=", "token=", "otptoken", "conmailer", "mailerlogin", "autologin", "auth=", "session")


# ------------------------------------------------------------------ helpers
def _lines(fragment):
    t = re.sub(r"<(br|/p|/div|/td|/tr|/li|/h\d|/span)[^>]*>", "\n", fragment, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return [" ".join(l.split()) for l in t.splitlines() if l.strip()]


def safe_link(url):
    """Drop query strings that carry login tokens."""
    if any(m in url.lower() for m in SECRET_MARKERS):
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}"
    return url


def _job(source, title, company="", loc="", link="", posted="", snippet="", uid=""):
    return {"title": title.strip(), "company_name": company.strip(), "location": loc.strip(),
            "apply_options": [{"title": source, "link": safe_link(link)}] if link else [],
            "source": source, "posted": posted.strip(), "snippet": snippet.strip(),
            "uid": f"{source}:{uid or link}"}


# ------------------------------------------------------------------ parsers
def parse_linkedin(plain, htm, src):
    jobs = []
    for j in lii.parse_alert_text(plain, htm):
        jobs.append(_job("LinkedIn", j["title"], j["company_name"], j["location"],
                         j["apply_options"][0]["link"], uid=j["linkedin_id"]))
    return jobs


def parse_indeed(plain, htm, src):
    jobs = []
    for block in re.split(r"\n\s*\n", plain):
        ls = [l.strip() for l in block.splitlines() if l.strip()]
        if len(ls) < 3 or "indeed." not in ls[-1] or "/clk" not in ls[-1]:
            continue
        url, title = ls[-1], ls[0]
        company, loc = (ls[1].rsplit(" - ", 1) + [""])[:2] if " - " in ls[1] else (ls[1], "")
        posted = ls[-2] if re.search(r"ago|just posted|today", ls[-2].lower()) else ""
        snippet = " ".join(x for x in ls[2:-2] if not re.search(r"[₹$€]|aed|a month|a year|easily apply|responsive employer", x.lower()))
        host = urlparse(url).netloc or "ae.indeed.com"
        jk = re.search(r"jk=([0-9a-f]{16})", url)
        link = f"https://{host}/viewjob?jk={jk.group(1)}" if jk else \
            f"https://{host}/jobs?q={quote(title + ' ' + company)}"
        jobs.append(_job("Indeed", title, company, loc, link, posted, snippet, uid=jk.group(1) if jk else title + company))
    return jobs


NG_SLUG = re.compile(r"naukrigulf\.com/([a-z0-9-]+?-jid-\d+)")


def parse_naukrigulf(plain, htm, src):
    jobs, seen = [], set()
    for raw in re.findall(r'href="([^"]+)"', htm) + re.findall(r"\((https?://[^)\s]+)\)", plain):
        u = raw
        for _ in range(4):
            u = unquote(html.unescape(u))
        m = NG_SLUG.search(u)
        if not m or m.group(1) in seen:
            continue
        slug = m.group(1)
        seen.add(slug)
        sm = re.match(r"(?P<t>.+?)-jobs-in-(?P<l>.+?)-in-(?P<c>.+?)-(?:\d+-to-\d+-years-)?n-cd-\d+-jid-(?P<j>\d+)$", slug)
        if sm:
            title = sm["t"].replace("-", " ").title()
            loc = sm["l"].replace("-", " ").title().replace("Uae", "UAE")
            company = sm["c"].replace("-", " ").title()
        else:
            title, loc, company = slug.split("-jid-")[0].replace("-", " ").title(), "", ""
        jobs.append(_job("Naukrigulf", title, company, loc, f"https://www.naukrigulf.com/{slug}", uid=slug))
    return jobs


def parse_gulftalent(plain, htm, src):
    jobs, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="(https?://www\.gulftalent\.com/[a-z-]+/jobs/[^"?#]+)[^"]*"[^>]*>(.*?)</a>',
                         htm, re.S | re.I):
        link = m.group(1)
        if link in seen:
            continue
        ls = [l for l in _lines(m.group(2)) if l.lower() not in ("apply now", "easy apply")]
        if not ls:
            continue
        seen.add(link)
        title = ls[0]
        company = ls[1] if len(ls) > 1 else ""
        loc = ls[2] if len(ls) > 2 else ""
        posted = next((x for x in ls if re.search(r"ago|today|yesterday", x.lower())), "")
        jobs.append(_job("GulfTalent", title, company, loc, link, posted, uid=link))
    return jobs


STOP = ("view all", "see all", "apply", "unsubscribe", "search", "privacy", "terms", "help", "download",
        "manage", "log in", "login", "sign in", "update", "profile", "settings", "read more", "learn more",
        "view job", "view more", "click here", "here", "facebook", "twitter", "linkedin", "instagram")


def parse_generic(plain, htm, src):
    """Any site: links on the site's own domain whose path looks like a job page."""
    dom = src.get("domain", "")
    jobs, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', htm, re.S | re.I):
        href = html.unescape(m.group(1))
        for _ in range(3):
            if "http" in href[5:] and ("url=" in href or "redirect=" in href or "u=" in href):
                inner = re.search(r"(?:url|redirect|u)=(https?[^&]+)", href)
                href = unquote(inner.group(1)) if inner else href
        host = urlparse(href).netloc.lower()
        if dom and dom not in host:
            continue
        if not re.search(r"job|vacanc|career|position", urlparse(href).path.lower()):
            continue
        text = " ".join(_lines(m.group(2)))
        if not (8 <= len(text) <= 120) or text.lower().startswith(STOP):
            continue
        clean = safe_link(href.split("#")[0])
        if clean in seen:
            continue
        seen.add(clean)
        jobs.append(_job(src["name"], text, "", "", clean, uid=clean))
    return jobs


PARSERS = {"linkedin": parse_linkedin, "indeed": parse_indeed, "naukrigulf": parse_naukrigulf,
           "gulftalent": parse_gulftalent, "generic": parse_generic}


# ------------------------------------------------------------------ Gmail
def _source_for(sender, sources):
    s = sender.lower()
    for src in sources:
        if any(f.lower() in s for f in src["from"]):
            return src
    return None


def gmail_alert_jobs(sources=None, days=3):
    sources = sources or DEFAULT_SOURCES
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        print("Gmail: not set up (GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets missing) - skipping")
        return [], {}
    jobs, per_source = [], {}
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com")
        box.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        ok, _ = box.select('"[Gmail]/All Mail"', readonly=True)
        if ok != "OK":
            box.select("INBOX", readonly=True)
        senders = " OR ".join(f for s in sources for f in s["from"])
        typ, data = box.search(None, "X-GM-RAW", f'"from:({senders}) newer_than:{days}d"')
        nums = data[0].split() if typ == "OK" and data and data[0] else []
        print(f"Gmail: {len(nums)} job-alert email(s) in the last {days} days")
        for num in nums[-80:]:
            typ, raw = box.fetch(num, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            src = _source_for(msg.get("From", ""), sources)
            if not src:
                continue
            plain, htm = lii._parts(msg)
            try:
                found = PARSERS.get(src.get("parser", "generic"), parse_generic)(plain, htm, src)
            except Exception as ex:
                ERRORS.append(f"{src['name']} email could not be read: {ex}")
                found = []
            per_source[src["name"]] = per_source.get(src["name"], 0) + len(found)
            jobs += found
        box.logout()
    except Exception as ex:
        ERRORS.append(f"Gmail: {ex} (check GMAIL_ADDRESS / GMAIL_APP_PASSWORD secrets)")
        print("  ! Gmail error:", ex)
    print("Jobs found in emails:", per_source or "none")
    return jobs, per_source
