"""The qualification gate — the only thing allowed to call a lead "qualified".

RevenueOS never synthesises a metric, and "N qualified prospects found" is a metric a
paying business will act on. So qualification is computed here, deterministically, from
the lead's own fields; it is never read from a file column (`qualified_at` in an import is
ignored) and never inferred from a signal like "forked a repo".

A lead is QUALIFIED only when all three hold:
  1. a business contact email — present, not a free-mail address (gmail, yahoo, ...);
  2. a business website — present, not a platform profile (github, twitter, youtube, ...);
  3. a company name that names a business — not an employer field scraped from a profile
     ("google", "Tiktok", "sugarcrm @IBM @Citigroup @GE"), not a handle, not a placeholder.

CONTACTABLE means only that some email address is present. FOUND means the row exists.
The three numbers are reported separately; none of them stands in for another.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "hotmail.com", "hotmail.co.uk", "outlook.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com",
    "gmx.de", "gmx.net", "web.de", "mail.com", "mail.ru", "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com",
    "naver.com", "hey.com", "fastmail.com", "zoho.com", "tutanota.com", "tuta.io", "duck.com", "example.com",
})

# Hosts that are profiles or platforms, never a business's own website.
PLATFORM_HOSTS = (
    "github.com", "github.io", "gitlab.com", "bitbucket.org", "twitter.com", "x.com", "t.co", "youtube.com", "youtu.be",
    "linkedin.com", "facebook.com", "fb.com", "instagram.com", "tiktok.com", "medium.com", "substack.com", "dev.to",
    "hashnode.dev", "reddit.com", "news.ycombinator.com", "stackoverflow.com", "kaggle.com", "huggingface.co",
    "linktr.ee", "bento.me", "about.me", "carrd.co", "notion.site", "notion.so", "t.me", "telegram.me", "discord.gg",
    "discord.com", "wa.me", "apps.apple.com", "play.google.com", "npmjs.com", "pypi.org", "crates.io", "scholar.google.com",
    "orcid.org", "researchgate.net", "behance.net", "dribbble.com", "vercel.app", "netlify.app", "herokuapp.com",
    "pages.dev", "web.app", "firebaseapp.com", "readthedocs.io", "wixsite.com", "sites.google.com",
)

# Employer fields people put on public profiles. A lead whose "company" is one of these is an
# employee of a large company, not a small business with a revenue problem and a budget.
EMPLOYER_NAMES = frozenset({
    "google", "alphabet", "youtube", "tiktok", "bytedance", "meta", "facebook", "instagram", "whatsapp", "apple", "amazon",
    "aws", "microsoft", "github", "linkedin", "openai", "anthropic", "nvidia", "intel", "amd", "ibm", "oracle", "sap",
    "salesforce", "adobe", "cisco", "dell", "hp", "vmware", "netflix", "uber", "lyft", "airbnb", "stripe", "shopify",
    "twitter", "x", "snap", "snapchat", "pinterest", "spotify", "tesla", "spacex", "samsung", "sony", "huawei", "xiaomi",
    "alibaba", "tencent", "baidu", "jd", "citigroup", "citi", "ge", "general electric", "jpmorgan", "goldman sachs",
    "morgan stanley", "accenture", "deloitte", "pwc", "kpmg", "ey", "mckinsey", "bcg", "bain", "infosys", "wipro", "tcs",
    "tata consultancy services", "hcl", "cognizant", "capgemini", "epam", "globant", "thoughtworks", "dataart", "endava",
    "luxoft", "softserve", "grid dynamics", "framgia", "sun asterisk", "sugarcrm", "cujo ai", "cujo", "atlassian",
    "twilio", "datadog", "snowflake", "databricks", "palantir", "cloudflare", "mongodb", "elastic", "hashicorp", "red hat",
    "canonical", "mozilla", "wikimedia", "nasa", "cern", "mit", "stanford", "harvard", "berkeley", "oxford", "cambridge",
    "student", "freelance", "freelancer", "self-employed", "self employed", "independent", "none", "n/a", "na", "home",
    "cloud", "remote", "earth", "internet", "world", "universe", "personal", "myself", "me", "open source", "opensource",
})

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")
HANDLE_RE = re.compile(r"^[a-z0-9_.-]{2,}$")  # "oxyz-official", "dxxx1988", "m3ta-hu3man" — a handle, not a name
DIGIT_LEET_RE = re.compile(r"[a-z]\d|\d[a-z]")  # "m3ta hu3man 0s"

QUALIFIED_STATUSES = frozenset({"scored", "drafted", "sent", "opened", "replied", "booked"})


@dataclass
class Qualification:
    contactable: bool
    business_email: bool
    has_website: bool
    company_ok: bool
    reasons: list[str] = field(default_factory=list)  # why it failed; empty when qualified

    @property
    def qualified(self) -> bool:
        return self.business_email and self.has_website and self.company_ok

    def as_dict(self) -> dict[str, Any]:
        return {"qualified": self.qualified, "contactable": self.contactable, "business_email": self.business_email,
                "has_website": self.has_website, "company_ok": self.company_ok, "reasons": list(self.reasons)}


# ── field normalisation ─────────────────────────────────────────────────────
def email_domain(email: str) -> str | None:
    m = EMAIL_RE.match((email or "").strip())
    return m.group(1).lower() if m else None


def website_host(url: str) -> str | None:
    """'www.beenoob.com' → 'beenoob.com'; 'https://x.com/foo' → 'x.com'; '' → None."""
    u = (url or "").strip()
    if not u or EMAIL_RE.match(u):
        return None
    u = re.sub(r"^[a-z]+://", "", u, flags=re.I)
    host = u.split("/")[0].split("?")[0].split(":")[0].lower().removeprefix("www.")
    return host if "." in host and " " not in host else None


def is_platform_host(host: str | None) -> bool:
    return bool(host) and any(host == p or host.endswith("." + p) for p in PLATFORM_HOSTS)


def clean_company(raw: str | None) -> tuple[str, str | None]:
    """Return (cleaned name, problem). `problem` is None when the name looks like a business.

    Fixes what profile scrapes produce: braces and leading @ ('{acmeAI}', '@acme'), URLs
    ('https://robotics.zhinno.com' → 'robotics.zhinno.com'), multi-employer strings
    ('sugarcrm @IBM @Citigroup @GE'), handles ('oxyz-official', 'dXXX1988'), employer names."""
    name = re.sub(r"\s+", " ", (raw or "")).strip().strip("{}[]()\"'").strip()
    name = name.lstrip("@").strip()
    if not name:
        return "", "no company name"
    if "@" in name and not EMAIL_RE.match(name):
        return name, "employer list from a profile, not a business"
    host = website_host(name)
    if host and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(/.*)?", name.lower().removeprefix("https://").removeprefix("http://")):
        # a bare domain is an acceptable business name; keep the owner's casing ("Tassos.gr")
        name = re.sub(r"^[a-z]+://", "", name, flags=re.I).removeprefix("www.").split("/")[0]
    low = name.lower()
    if low in EMPLOYER_NAMES or low.rstrip(".") in EMPLOYER_NAMES:
        return name, f"employer field ({name}), not a small business"
    if DIGIT_LEET_RE.search(low) and not host:
        return name, f"looks like a handle ({name})"
    if HANDLE_RE.match(low) and not host and (any(ch.isdigit() for ch in low) or (name == low and ("-" in low or "_" in low))):
        return name, f"looks like a handle ({name})"  # 'oxyz-official', 'dxxx1988' — but not 'Baxter-Brunello'
    if len(low) < 2:
        return name, "company name too short"
    return name, None


def normalise_row(row: dict[str, Any]) -> dict[str, Any]:
    """Fix field mapping from profile scrapes before anything reads the row.

    - an email address in the website column moves to `email` (when email is empty);
    - a platform profile URL in `website` moves to `profile_url`, leaving website empty;
    - `qualified_at` is dropped: imports do not get to assert qualification."""
    r = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
    site = r.get("website") or ""
    if EMAIL_RE.match(site):
        if not r.get("email"):
            r["email"] = site.lower()
        r["website"] = ""
    host = website_host(r.get("website") or "")
    if host and is_platform_host(host):
        r.setdefault("profile_url", r["website"])
        r["website"] = ""
    elif host is None:
        r["website"] = ""
    elif not re.match(r"^[a-z]+://", r["website"], flags=re.I):
        r["website"] = "https://" + r["website"].removeprefix("www.")
    if r.get("email"):
        r["email"] = r["email"].lower() if EMAIL_RE.match(r["email"]) else ""
    r.pop("qualified_at", None)
    return r


def qualify(row: dict[str, Any]) -> Qualification:
    """The gate. `row` uses the lead-sheet column names (email, company, website)."""
    r = normalise_row(row)
    reasons: list[str] = []
    dom = email_domain(r.get("email") or "")
    contactable = dom is not None
    business_email = contactable and dom not in FREEMAIL_DOMAINS
    if not contactable:
        reasons.append("no contact email")
    elif not business_email:
        reasons.append(f"personal address ({dom}), not a business email")
    host = website_host(r.get("website") or "")
    has_website = host is not None and not is_platform_host(host)
    if not has_website:
        reasons.append("no business website" + (" (profile URL only)" if r.get("profile_url") else ""))
    _name, problem = clean_company(r.get("company") or r.get("business_name"))
    company_ok = problem is None
    if problem:
        reasons.append(problem)
    return Qualification(contactable, business_email, has_website, company_ok, reasons)
