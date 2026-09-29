"""
UAE Job Alerts -> Telegram
--------------------------
1. Searches Google Jobs (SerpApi) for each title in config.json, UAE only.
2. Scores every job against your CV (cv.txt) - ATS-style keyword match.
3. Keeps only jobs with score >= min_ats_score (default 70).
4. Looks up public company info: website, phone, address (Google Maps via
   SerpApi) and role-based emails (hr@, careers@, info@ ...) from the
   company website and the job post.
5. Sends each job as a separate Telegram message.
6. Remembers jobs already sent (seen_jobs.json) and companies already
   looked up (companies.json) so nothing repeats and API usage stays low.

GitHub Secrets needed: SERPAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import hashlib, html, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

BASE = Path(__file__).parent
CONFIG = BASE / "config.json"
CV_FILE = BASE / "cv.txt"
SEEN_FILE = BASE / "seen_jobs.json"
COMPANY_FILE = BASE / "companies.json"
SKILLS_FILE = BASE / "skills.txt"

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ROLE_PREFIXES = ("hr", "careers", "career", "jobs", "job", "recruit", "recruitment",
                 "talent", "hiring", "info", "contact", "enquir", "inquir", "admin",
                 "people", "cv", "resume", "apply", "vacanc", "hello", "office")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", ".png", ".jpg", ".jpeg", ".gif",
                   ".webp", ".svg", "domain.com", "email.com", "yourname", "@2x")


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


def job_key(job):
    raw = f"{job.get('title','')}|{job.get('company_name','')}|{job.get('location','')}".lower()
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- ATS score
def load_skills():
    """skills.txt: one skill per line. Synonyms separated by | (first is the label)."""
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


def ats_score(job, cv_norm, skills, target_titles):
    """
    Score 0-100, like an ATS keyword screen:
      70% = of the skills this job asks for, how many are on your CV
      30% = how well the job title matches your target titles
    """
    jd = norm(f"{job.get('title','')} {job.get('description','')} "
              f"{' '.join(h for hl in job.get('job_highlights', []) for h in hl.get('items', []))}")
    job_skills = [(label, v) for label, v in skills if found(v, jd)]
    have = [label for label, v in job_skills if found(v, cv_norm)]
    missing = [label for label, v in job_skills if label not in have]

    if len(job_skills) >= 3:
        skill_pct = len(have) / len(job_skills)
    else:  # job ad too short to judge on skills - be neutral
        skill_pct = 0.6

    title_n = norm(job.get("title", ""))
    title_pct = 0.0
    for t in target_titles:
        words = [w for w in norm(t).split() if len(w) > 2]
        if not words:
            continue
        hit = sum(1 for w in words if f" {w} " in title_n) / len(words)
        title_pct = max(title_pct, hit)

    score = round(100 * (0.7 * skill_pct + 0.3 * title_pct))
    return score, have, missing


# ---------------------------------------------------------------- job search
def serpapi(params):
    params = {**params, "api_key": SERPAPI_KEY}
    r = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or "error" in data:
        print(f"  ! SerpApi: {data.get('error', r.status_code)}")
        return {}
    return data


def search_jobs(query, location):
    data = serpapi({"engine": "google_jobs", "q": query, "location": location,
                    "gl": "ae", "hl": "en"})
    return data.get("jobs_results", [])


# ---------------------------------------------------------------- company info
def clean_emails(emails, allow_personal=False):
    out = []
    for e in emails:
        e = e.strip(".").lower()
        if any(b in e for b in BAD_EMAIL_PARTS):
            continue
        local = e.split("@")[0]
        if not allow_personal and not local.startswith(ROLE_PREFIXES):
            continue
        if e not in out:
            out.append(e)
    return out


def emails_from_site(website):
    """Public role-based emails from the company's homepage / contact / careers pages."""
    found_emails = []
    if not website:
        return found_emails
    pages = [website] + [urljoin(website, p) for p in
                         ("/contact", "/contact-us", "/careers", "/contactus")]
    for url in pages:
        try:
            r = requests.get(url, headers=UA, timeout=12)
            if r.ok and "text/html" in r.headers.get("content-type", ""):
                found_emails += EMAIL_RE.findall(r.text)
        except requests.RequestException:
            pass
        if len(clean_emails(found_emails)) >= 3:
            break
    return clean_emails(found_emails)[:4]


def company_info(name, cache):
    key = name.lower().strip()
    if key in cache:
        return cache[key]
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
def fmt(job, score, have, missing, info, post_emails):
    e = html.escape
    ext = job.get("detected_extensions", {}) or {}
    lines = [f"<b>{e(job.get('title', 'Untitled'))}</b>",
             f"🏢 {e(job.get('company_name', ''))}",
             f"📍 {e(job.get('location', ''))}"]
    meta = " · ".join(x for x in [ext.get("schedule_type", ""), ext.get("posted_at", ""),
                                  ext.get("salary", "")] if x)
    if meta:
        lines.append(f"🕒 {e(meta)}")
    lines.append(f"🎯 <b>ATS match: {score}%</b>")
    if have:
        lines.append(f"✅ {e(', '.join(have[:8]))}")
    if missing:
        lines.append(f"⚠️ Missing: {e(', '.join(missing[:6]))}")

    links = job.get("apply_options") or []
    if links:
        lines += ["", "<b>Apply links:</b>"]
        for o in links[:5]:
            if o.get("link"):
                lines.append(f'• <a href="{e(o["link"], quote=True)}">{e(o.get("title", "Apply"))}</a>')
    elif job.get("share_link"):
        lines += ["", f'<a href="{e(job["share_link"], quote=True)}">View job</a>']

    emails = list(dict.fromkeys(post_emails + (info or {}).get("emails", [])))
    if info and any([info.get("website"), info.get("phone"), info.get("address"), emails]):
        lines += ["", "<b>Company info:</b>"]
        if info.get("website"):
            lines.append(f'🌐 <a href="{e(info["website"], quote=True)}">{e(urlparse(info["website"]).netloc or info["website"])}</a>')
        if info.get("phone"):
            lines.append(f"📞 {e(info['phone'])}")
        for m in emails[:4]:
            lines.append(f"✉️ {e(m)}")
        if info.get("address"):
            lines.append(f"🏠 {e(info['address'])}")
        if info.get("rating"):
            lines.append(f"⭐ {e(info['rating'])}")
    elif emails:
        lines += ["", "<b>Contact:</b>"] + [f"✉️ {e(m)}" for m in emails[:4]]
    return "\n".join(lines)


def send(text):
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "HTML",
                                "disable_web_page_preview": True}, timeout=30)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 30) + 1)
            continue
        if not r.ok:
            print(f"  ! Telegram {r.status_code}: {r.text[:200]}")
        return r.ok
    return False


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
    exclude = [w.lower() for w in cfg.get("exclude_words", [])]
    seen = set(load_json(SEEN_FILE, []))
    companies = load_json(COMPANY_FILE, {})

    matches = []
    for title in titles:
        for loc in cfg.get("locations", ["United Arab Emirates"]):
            print(f"Searching: {title} | {loc}")
            for job in search_jobs(title, loc):
                k = job_key(job)
                if k in seen:
                    continue
                seen.add(k)
                text = f"{job.get('title','')} {job.get('description','')}".lower()
                if any(w in text for w in exclude):
                    continue
                score, have, miss = ats_score(job, cv_norm, skills, titles)
                print(f"  {score:3d}%  {job.get('title')} - {job.get('company_name')}")
                if score >= min_score:
                    matches.append((score, job, have, miss))

    matches.sort(key=lambda m: m[0], reverse=True)
    matches = matches[: cfg.get("max_jobs_per_run", 15)]
    print(f"Matches >= {min_score}%: {len(matches)}")

    lookups = 0
    sent = 0
    for score, job, have, miss in matches:
        info = None
        name = job.get("company_name", "")
        if name and (name.lower().strip() in companies or
                     lookups < cfg.get("max_company_lookups_per_run", 5)):
            if name.lower().strip() not in companies:
                lookups += 1
            info = company_info(name, companies)
        post_emails = clean_emails(EMAIL_RE.findall(job.get("description", "")), allow_personal=True)
        if send(fmt(job, score, have, miss, info, post_emails)):
            sent += 1
        time.sleep(3)

    if sent == 0 and cfg.get("notify_when_empty", True):
        send(f"No new UAE jobs with ATS ≥ {min_score}% today.")

    save_json(SEEN_FILE, sorted(seen)[-5000:])
    save_json(COMPANY_FILE, companies)
    print(f"Sent {sent} job(s). Company lookups used: {lookups}")


if __name__ == "__main__":
    main()
