#!/usr/bin/env python3
"""
Earendil - congressional trade tracker (v3.1)

Named for the mariner who carried a light to the powers that be on behalf of
everyone else.

Pulls STOCK Act periodic transaction reports (PTRs) for the House and Senate,
then adds accountability context to each trade:

  * Late filing        - disclosed more than 45 days after the transaction
  * Committee/sector   - the member sits on a committee that oversees the
                         traded company's sector
  * Committee timing   - that committee met within N days of the trade
                         (needs a free Congress.gov API key)
  * Performance        - how the stock moved afterward vs. SPY (approximate)

Trades are stored in data/trades.json. The report (docs/index.html) and a
CSV download (docs/trade_log.csv) are rebuilt after every run, ready for
GitHub Pages and installable on a phone home screen.

Flags describe patterns worth a closer look. They are not evidence of
wrongdoing, and the report says so.

Usage
  python earendil.py              run once
  python earendil.py --loop 60    run every 60 minutes
  python earendil.py --report     rebuild the report from saved data
  python earendil.py --demo       fictional sample data, writes docs/demo.html

Secrets can come from environment variables instead of the config file:
  FMP_API_KEY, CONGRESS_GOV_API_KEY, WEBHOOK_URL, NTFY_TOPIC, SMTP_PASSWORD
"""

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import os
import platform
import re
import smtplib
import subprocess
import sys
import time
import unicodedata
import zipfile
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

APP = "Earendil"
VERSION = "3.1"
BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "scraper_config.json"
DATA_DIR = BASE / "data"
SITE_DIR = BASE / "docs"
TRADES_PATH = DATA_DIR / "trades.json"
CSV_PATH = SITE_DIR / "trade_log.csv"
REPORT_PATH = SITE_DIR / "index.html"
DEMO_PATH = SITE_DIR / "demo.html"
CACHE_DIR = DATA_DIR / "cache"

log = logging.getLogger("earendil")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "data_source": "fmp",                     # "fmp" (trade-level) or "portal" (filing alerts only)
    "fmp_api_key": "",
    "fmp_endpoints": {
        "senate": "https://financialmodelingprep.com/stable/senate-latest",
        "house": "https://financialmodelingprep.com/stable/house-latest",
    },
    "fmp_pages": 5,                           # 100 trades per page per chamber
    "coverage": "all",                        # "all" or "watchlist"
    "watch_list": [],                         # e.g. ["Nancy Pelosi", "Mike Crapo"]
    "ownership_filter": ["self", "spouse", "joint", "dependent", "unknown"],
    "flags": {
        "late_filing": True,
        "committee_sector": True,
        "committee_timing": False,
        "performance": False,
    },
    "late_filing_days": 45,
    "congress_gov_api_key": "",
    "timing_window_days": 14,
    "max_meeting_fetches_per_run": 400,
    "performance_horizon_days": 30,
    "performance_flag_pct": 10,
    "max_lookups_per_run": 150,               # caps sector + price API calls
    "sector_overrides": {},                   # {"TICKER": "Industrials"}
    "alert_on": "flagged",                    # "all", "flagged", or "watchlist"
    "alerts": {
        "desktop": True,                      # ignored when running on GitHub Actions
        "ntfy_topic": "",                     # phone push via the free ntfy app
        "webhook_url": "",
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "",
            "password": "",
            "to": "",
        },
    },
    "report": {
        "enabled": True,
        "title": "Earendil",
        "subtitle": "Congressional trades, with context",
        "focus_state": "ID",                  # two-letter code, or "" for none
        "site_url": "",                       # your GitHub Pages link, used in push alerts
    },
}


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def apply_env(cfg):
    """Secrets from the environment (GitHub Actions secrets) win over the file."""
    env = os.environ.get
    if env("FMP_API_KEY"):
        cfg["fmp_api_key"] = env("FMP_API_KEY")
    if env("CONGRESS_GOV_API_KEY"):
        cfg["congress_gov_api_key"] = env("CONGRESS_GOV_API_KEY")
    if env("WEBHOOK_URL"):
        cfg["alerts"]["webhook_url"] = env("WEBHOOK_URL")
    if env("NTFY_TOPIC"):
        cfg["alerts"]["ntfy_topic"] = env("NTFY_TOPIC")
    if env("SMTP_PASSWORD"):
        cfg["alerts"]["email"]["password"] = env("SMTP_PASSWORD")
    return cfg


def check_flag_keys(cfg):
    """Switch off flags whose API key is missing, loudly, so the report never advertises
    a check that can't run. (Demo mode turns everything back on with fake data.)"""
    flags = cfg["flags"]
    if flags.get("committee_timing") and not cfg["congress_gov_api_key"]:
        log.warning("Committee timing is ON but no Congress.gov key is set (CONGRESS_GOV_API_KEY). "
                    "Turning it off for this run. Get a free key at api.congress.gov/sign-up.")
        flags["committee_timing"] = False
    if not cfg["fmp_api_key"]:
        for name, label in (("performance", "Well-timed"), ("committee_sector", "Committee oversight")):
            if flags.get(name):
                log.warning("%s needs an FMP key for market data (FMP_API_KEY); turning it off "
                            "for this run.", label)
                flags[name] = False
    return cfg


def load_config(path=CONFIG_PATH):
    if path.exists():
        with open(path) as f:
            user = json.load(f)
        # v2 compatibility: a watch list with no coverage key meant watch-list mode
        if "coverage" not in user and user.get("watch_list"):
            user["coverage"] = "watchlist"
        if "title" in user.get("report", {}) and "subtitle" not in user["report"]:
            user["report"]["subtitle"] = user["report"]["title"]
            user["report"]["title"] = APP
        return apply_env(deep_merge(DEFAULT_CONFIG, user))
    log.warning("No scraper_config.json found - using defaults. Create one with settings.html.")
    return apply_env(json.loads(json.dumps(DEFAULT_CONFIG)))


# ---------------------------------------------------------------------------
# HTTP + small helpers
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers["User-Agent"] = f"earendil/{VERSION} (congressional trade accountability research)"


def http_get(url, params=None, retries=3, timeout=30):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            log.debug("Retrying %s after %s", url, e)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Rate limited too many times: {url}")


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    s = str(value).strip()[:19]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%y"):
        try:
            return datetime.strptime(s if "T" in fmt or " " in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def pick(d, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "--", "N/A"):
            return v
    return default


def norm_name(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|hon|dr|mr|mrs|ms)\b\.?", "", s.lower())
    return re.sub(r"[^a-z ]", "", s).strip()


def cache_json(name, url, max_age_days):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    if path.exists() and (time.time() - path.stat().st_mtime) < max_age_days * 86400:
        with open(path) as f:
            return json.load(f)
    data = http_get(url).json()
    with open(path, "w") as f:
        json.dump(data, f)
    return data


# ---------------------------------------------------------------------------
# Normalizing trades
# ---------------------------------------------------------------------------

OWNER_MAP = {
    "self": "self", "filer": "self",
    "spouse": "spouse", "sp": "spouse",
    "joint": "joint", "jt": "joint",
    "child": "dependent", "dependent": "dependent", "dc": "dependent", "dependent child": "dependent",
}


def norm_owner(raw):
    return OWNER_MAP.get(str(raw or "").strip().lower(), "unknown")


def norm_type(raw):
    s = str(raw or "").lower()
    if "purchase" in s or s in ("p", "buy"):
        return "purchase"
    if "sale" in s or s in ("s", "sell", "s (partial)", "s (full)"):
        return "sale"
    if "exchange" in s or s == "e":
        return "exchange"
    return s or "unknown"


def parse_amount(raw):
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(raw or ""))]
    if not nums:
        return None, None
    return nums[0], (nums[1] if len(nums) > 1 else None)


def trade_id(t):
    key = "|".join(str(t.get(k, "")) for k in (
        "chamber", "name", "ticker", "asset", "type", "amount",
        "transaction_date", "disclosure_date", "owner"))
    return hashlib.md5(key.encode()).hexdigest()


def core_key(t):
    """Identity of a trade apart from when it was disclosed, so an amended filing that
    repeats a trade matches the original. Only defined for trade-level records; portal
    records have no transaction details to match on."""
    if t.get("source") != "fmp" or not t.get("transaction_date"):
        return None
    return "|".join(str(t.get(k, "")).strip().lower() for k in (
        "chamber", "name", "ticker", "asset", "type", "amount", "transaction_date", "owner"))


def normalize_fmp(raw, chamber):
    first = pick(raw, "firstName", "first_name")
    last = pick(raw, "lastName", "last_name")
    name = f"{first} {last}".strip() or pick(raw, "representative", "senator", "office", "name")
    district = pick(raw, "district", "state")
    state = district[:2].upper() if district else ""
    low, high = parse_amount(pick(raw, "amount", "range"))
    t = {
        "chamber": chamber,
        "first": first,
        "last": last or (name.split()[-1] if name else ""),
        "name": name,
        "state": state,
        "district": district,
        "owner": norm_owner(pick(raw, "owner")),
        "ticker": str(pick(raw, "symbol", "ticker")).upper().strip(),
        "asset": pick(raw, "assetDescription", "asset_description", "asset"),
        "asset_type": pick(raw, "assetType", "asset_type"),
        "type": norm_type(pick(raw, "type", "transactionType")),
        "amount": pick(raw, "amount", "range"),
        "amount_low": low,
        "amount_high": high,
        "transaction_date": str(parse_date(pick(raw, "transactionDate", "transaction_date")) or ""),
        "disclosure_date": str(parse_date(pick(raw, "disclosureDate", "dateRecieved",
                                                  "dateReceived", "disclosure_date")) or ""),
        "link": pick(raw, "link", "ptr_link", "url"),
        "source": "fmp",
    }
    t["id"] = trade_id(t)
    return t


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def fetch_fmp(cfg):
    key = cfg["fmp_api_key"]
    if not key:
        raise RuntimeError("FMP API key missing. Set the FMP_API_KEY secret, or use data_source 'portal'.")
    trades = []
    for chamber, url in cfg["fmp_endpoints"].items():
        for page in range(int(cfg["fmp_pages"])):
            data = http_get(url, params={"page": page, "limit": 100, "apikey": key}).json()
            if isinstance(data, dict):
                if "Error Message" in data:
                    raise RuntimeError(f"FMP error: {data['Error Message']}")
                data = data.get("data") or data.get("results") or []
            if not data:
                break
            trades.extend(normalize_fmp(x, chamber) for x in data)
    log.info("FMP returned %d trade records", len(trades))
    return trades


def fetch_house_portal(year):
    """House Clerk yearly index. Filing-level only: PTR PDFs hold the transactions."""
    url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
    r = http_get(url, timeout=60)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(f"{year}FD.xml").decode("utf-8", "ignore")
    out = []
    for m in re.findall(r"<Member>(.*?)</Member>", xml, re.S):
        get = lambda tag: (re.search(f"<{tag}>(.*?)</{tag}>", m, re.S) or [None, ""])[1].strip()
        if get("FilingType") != "P":
            continue
        doc = get("DocID")
        t = {
            "chamber": "house", "first": get("First"), "last": get("Last"),
            "name": f"{get('First')} {get('Last')}".strip(), "state": get("StateDst")[:2],
            "district": get("StateDst"), "owner": "unknown", "ticker": "", "asset": "PTR filing",
            "asset_type": "", "type": "filing", "amount": "", "amount_low": None, "amount_high": None,
            "transaction_date": "", "disclosure_date": str(parse_date(get("FilingDate")) or ""),
            "link": f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf",
            "source": "house-portal",
        }
        t["id"] = trade_id(t)
        out.append(t)
    return out


def fetch_senate_portal(since):
    """Senate eFD search. Filing-level only."""
    home = "https://efdsearch.senate.gov/search/home/"
    s = requests.Session()
    s.headers["User-Agent"] = SESSION.headers["User-Agent"]
    page = s.get(home, timeout=30).text
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    if not token:
        raise RuntimeError("Senate eFD layout changed; could not find the agreement form.")
    s.post(home, data={"prohibition_agreement": "1", "csrfmiddlewaretoken": token.group(1)},
           headers={"Referer": home}, timeout=30)
    r = s.post("https://efdsearch.senate.gov/search/report/data/", timeout=30,
               headers={"Referer": "https://efdsearch.senate.gov/search/"},
               data={"start": "0", "length": "200", "report_types": "[11]", "filer_types": "[]",
                     "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"),
                     "submitted_end_date": "", "candidate_state": "", "senator_state": "",
                     "office_id": "", "first_name": "", "last_name": "",
                     "csrfmiddlewaretoken": s.cookies.get("csrftoken", "")})
    out = []
    for row in r.json().get("data", []):
        first, last, _office, link_html, filed = row[:5]
        href = re.search(r'href="([^"]+)"', link_html)
        t = {
            "chamber": "senate", "first": first, "last": last, "name": f"{first} {last}".strip(),
            "state": "", "district": "", "owner": "unknown", "ticker": "", "asset": "PTR filing",
            "asset_type": "", "type": "filing", "amount": "", "amount_low": None, "amount_high": None,
            "transaction_date": "", "disclosure_date": str(parse_date(filed) or ""),
            "link": ("https://efdsearch.senate.gov" + href.group(1)) if href else "",
            "source": "senate-portal",
        }
        t["id"] = trade_id(t)
        out.append(t)
    return out


def fetch_portal(cfg):
    trades = []
    year = date.today().year
    try:
        trades += fetch_house_portal(year)
    except Exception as e:
        log.error("House portal failed: %s", e)
    try:
        trades += fetch_senate_portal(date.today() - timedelta(days=60))
    except Exception as e:
        log.error("Senate portal failed: %s", e)
    log.info("Portals returned %d filings (no transaction details, so flags are limited)", len(trades))
    return trades


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def on_watch_list(t, watch):
    name = norm_name(t["name"])
    watch = [w.get("name", "") if isinstance(w, dict) else str(w) for w in watch]
    return any(all(tok in name.split() for tok in norm_name(w).split()) for w in watch if w.strip())


def apply_filters(trades, cfg):
    owners = set(cfg["ownership_filter"])
    watch = cfg.get("watch_list") or []
    kept = []
    for t in trades:
        t["watched"] = on_watch_list(t, watch)
        if t["source"] == "fmp" and t["owner"] not in owners:
            continue
        if cfg["coverage"] == "watchlist" and not t["watched"]:
            continue
        kept.append(t)
    return kept


# ---------------------------------------------------------------------------
# Committee roster (open data from the @unitedstates congress-legislators project)
# ---------------------------------------------------------------------------

ROSTER_BASE = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/"

# Which FMP sectors each full committee oversees. A judgment call - edit freely.
# Appropriations, Budget, Rules, Ethics, and Oversight are left out on purpose:
# their reach covers everything, so a match would mean nothing.
COMMITTEE_SECTORS = {
    # Senate
    "SSAF": ["Consumer Defensive", "Basic Materials"],                         # Agriculture
    "SSAS": ["Industrials", "Technology"],                                     # Armed Services
    "SSBK": ["Financial Services", "Real Estate"],                             # Banking
    "SSCM": ["Communication Services", "Technology", "Industrials", "Consumer Cyclical"],  # Commerce
    "SSEG": ["Energy", "Utilities", "Basic Materials"],                        # Energy & Natural Resources
    "SSEV": ["Utilities", "Industrials", "Basic Materials"],                   # Environment & Public Works
    "SSFI": ["Healthcare", "Financial Services"],                              # Finance
    "SSHR": ["Healthcare"],                                                    # HELP
    "SSGA": ["Technology", "Industrials"],                                     # Homeland Security
    "SSJU": ["Technology", "Communication Services"],                          # Judiciary (antitrust)
    "SLIN": ["Technology", "Industrials"],                                     # Intelligence
    "SSVA": ["Healthcare"],                                                    # Veterans' Affairs
    # House
    "HSAG": ["Consumer Defensive", "Basic Materials"],                         # Agriculture
    "HSAS": ["Industrials", "Technology"],                                     # Armed Services
    "HSBA": ["Financial Services", "Real Estate"],                             # Financial Services
    "HSIF": ["Healthcare", "Energy", "Utilities", "Communication Services",
             "Technology", "Consumer Cyclical"],                               # Energy & Commerce
    "HSPW": ["Industrials"],                                                   # Transportation & Infrastructure
    "HSSY": ["Technology", "Industrials", "Energy"],                           # Science, Space & Technology
    "HSWM": ["Healthcare", "Financial Services"],                              # Ways & Means
    "HSHM": ["Technology", "Industrials"],                                     # Homeland Security
    "HSJU": ["Technology", "Communication Services"],                          # Judiciary
    "HSII": ["Energy", "Basic Materials"],                                     # Natural Resources
    "HLIG": ["Technology", "Industrials"],                                     # Intelligence
    "HSVR": ["Healthcare"],                                                    # Veterans' Affairs
}


class Roster:
    def __init__(self):
        legislators = cache_json("legislators-current.json", ROSTER_BASE + "legislators-current.json", 7)
        membership = cache_json("committee-membership-current.json",
                                ROSTER_BASE + "committee-membership-current.json", 7)
        committees = cache_json("committees-current.json", ROSTER_BASE + "committees-current.json", 7)

        self.committee_names = {c["thomas_id"]: c["name"] for c in committees}
        self.people, self.by_key, self.by_last = {}, {}, {}
        for p in legislators:
            bio = p["id"]["bioguide"]
            term = p["terms"][-1]
            chamber = "house" if term["type"] == "rep" else "senate"
            last = norm_name(p["name"]["last"])
            self.people[bio] = {
                "first": norm_name(p["name"].get("first")),
                "nick": norm_name(p["name"].get("nickname", "")),
                "state": term["state"],
                "name": p["name"].get("official_full") or f"{p['name']['first']} {p['name']['last']}",
            }
            self.by_key.setdefault((chamber, last, term["state"]), []).append(bio)
            self.by_last.setdefault((chamber, last), []).append(bio)

        self.committees = {}
        for code, members in membership.items():
            top = code[:4]
            for m in members:
                if m.get("bioguide"):
                    self.committees.setdefault(m["bioguide"], set()).add(top)

    def resolve(self, t):
        last = norm_name(t["last"]).split()
        if not last:
            return None
        last = last[-1] if (t["chamber"], " ".join(last)) not in self.by_last else " ".join(last)
        cands = self.by_key.get((t["chamber"], last, t["state"])) if t["state"] else None
        if not cands:
            cands = self.by_last.get((t["chamber"], last), [])
        if len(cands) > 1 and t.get("first"):
            f = norm_name(t["first"])[:3]
            cands = [b for b in cands if self.people[b]["first"].startswith(f)
                     or self.people[b]["nick"].startswith(f)]
        return cands[0] if len(cands) == 1 else None

    def committees_for(self, bioguide):
        return sorted(self.committees.get(bioguide, set()))

    def state_for(self, bioguide):
        return self.people.get(bioguide, {}).get("state", "")


# ---------------------------------------------------------------------------
# Market data (FMP) with on-disk caching
# ---------------------------------------------------------------------------

class Market:
    def __init__(self, cfg):
        self.key = cfg["fmp_api_key"]
        self.overrides = {k.upper(): v for k, v in cfg.get("sector_overrides", {}).items()}
        self.budget = int(cfg["max_lookups_per_run"])
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.sector_path = CACHE_DIR / "sectors.json"
        self.sectors = json.loads(self.sector_path.read_text()) if self.sector_path.exists() else {}
        self.prices = {}

    def _spend(self):
        if self.budget <= 0 or not self.key:
            return False
        self.budget -= 1
        return True

    def sector(self, ticker):
        if not ticker or not re.fullmatch(r"[A-Z.\-]{1,6}", ticker):
            return ""
        if ticker in self.overrides:
            return self.overrides[ticker]
        if ticker in self.sectors:
            return self.sectors[ticker]
        if not self._spend():
            return ""
        try:
            data = http_get("https://financialmodelingprep.com/stable/profile",
                            params={"symbol": ticker, "apikey": self.key}).json()
            row = data[0] if isinstance(data, list) and data else {}
            self.sectors[ticker] = row.get("sector", "") or ""
        except Exception as e:
            log.debug("Sector lookup failed for %s: %s", ticker, e)
            return ""
        self.sector_path.write_text(json.dumps(self.sectors))
        return self.sectors[ticker]

    def series(self, ticker, start):
        if ticker in self.prices:
            return self.prices[ticker]
        if not self._spend():
            return {}
        try:
            data = http_get("https://financialmodelingprep.com/stable/historical-price-eod/light",
                            params={"symbol": ticker, "from": str(start - timedelta(days=7)),
                                    "apikey": self.key}).json()
            rows = data.get("historical", []) if isinstance(data, dict) else data
            s = {r["date"]: float(r.get("price", r.get("close"))) for r in rows
                 if r.get("price", r.get("close")) is not None}
        except Exception as e:
            log.debug("Price lookup failed for %s: %s", ticker, e)
            s = {}
        self.prices[ticker] = s
        return s

    def forward_return(self, ticker, d0, d1):
        s = self.series(ticker, d0)
        if not s:
            return None
        dates = sorted(s)
        start = next((d for d in dates if d >= str(d0)), None)
        end = next((d for d in reversed(dates) if d <= str(d1)), None)
        if not start or not end or end <= start:
            return None
        return s[end] / s[start] - 1


# ---------------------------------------------------------------------------
# Committee meetings (Congress.gov API) with incremental caching
# ---------------------------------------------------------------------------

def congress_number(d):
    return (d.year - 1789) // 2 + 1


class Meetings:
    API = "https://api.congress.gov/v3/committee-meeting"

    def __init__(self, cfg):
        self.key = cfg["congress_gov_api_key"]
        self.budget = int(cfg["max_meeting_fetches_per_run"])
        self.by_code = {}
        if not self.key:
            log.warning("Committee timing is on but no Congress.gov API key is set - skipping.")
            return
        for congress in sorted({congress_number(date.today()), congress_number(date.today()) - 1}):
            self._sync(congress)

    def _sync(self, congress):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"meetings_{congress}.json"
        idx = json.loads(path.read_text()) if path.exists() else {"events": {}}
        pending = []
        for chamber in ("house", "senate"):
            offset = 0
            while True:
                try:
                    data = http_get(f"{self.API}/{congress}/{chamber}", params={
                        "api_key": self.key, "format": "json", "limit": 250, "offset": offset}).json()
                except Exception as e:
                    log.error("Congress.gov listing failed: %s", e)
                    break
                items = data.get("committeeMeetings", [])
                pending += [(chamber, str(i["eventId"])) for i in items
                            if str(i["eventId"]) not in idx["events"]]
                if len(items) < 250:
                    break
                offset += 250
        still_syncing = len(pending) > self.budget
        for chamber, eid in pending[: self.budget]:
            try:
                m = http_get(f"{self.API}/{congress}/{chamber}/{eid}",
                             params={"api_key": self.key, "format": "json"}).json().get("committeeMeeting", {})
                idx["events"][eid] = {
                    "date": str(parse_date(m.get("date")) or ""),
                    "codes": sorted({c.get("systemCode", "")[:4].upper() for c in m.get("committees", [])}),
                    "title": (m.get("title") or "")[:200],
                }
            except Exception as e:
                log.debug("Meeting %s failed: %s", eid, e)
        self.budget -= min(len(pending), self.budget)
        if still_syncing:
            log.info("Committee meetings for the %dth Congress still syncing; more each run.", congress)
        path.write_text(json.dumps(idx))
        for ev in idx["events"].values():
            for code in ev["codes"]:
                if ev["date"]:
                    self.by_code.setdefault(code, []).append(ev)

    def nearest(self, code, d, window):
        best = None
        for ev in self.by_code.get(code, []):
            gap = (parse_date(ev["date"]) - d).days
            if abs(gap) <= window and (best is None or abs(gap) < abs(best[0])):
                best = (gap, ev)
        return best


# ---------------------------------------------------------------------------
# Enrichment: the accountability flags
# ---------------------------------------------------------------------------

def add_flag(t, code, label, detail):
    t["flags"].append({"code": code, "label": label, "detail": detail})


def enrich(trades, cfg, roster=None, market=None, meetings=None):
    flags = cfg["flags"]
    limit = int(cfg["late_filing_days"])
    window = int(cfg["timing_window_days"])
    for t in trades:
        t["flags"] = []
        t.setdefault("sector", "")
        t.setdefault("committees", [])
        t.setdefault("perf", None)
        tx, disc = parse_date(t["transaction_date"]), parse_date(t["disclosure_date"])

        # 1. Late filing
        if flags.get("late_filing") and tx and disc:
            lag = (disc - tx).days
            t["filing_lag_days"] = lag
            if lag > limit:
                add_flag(t, "late", "Late filing",
                         f"Disclosed {lag} days after the trade; the STOCK Act deadline is {limit}.")

        # Member and sector context
        if roster:
            bio = roster.resolve(t)
            t["bioguide"] = bio or ""
            if bio:
                t["committees"] = roster.committees_for(bio)
                t["state"] = t["state"] or roster.state_for(bio)
        if market and t["ticker"]:
            t["sector"] = t["sector"] or market.sector(t["ticker"])

        # 2. Committee oversees the traded sector
        overlap = [c for c in t["committees"] if t["sector"] in COMMITTEE_SECTORS.get(c, [])]
        if flags.get("committee_sector") and overlap and roster:
            names = ", ".join(roster.committee_names.get(c, c) for c in overlap)
            add_flag(t, "committee", "Committee oversight",
                     f"{t['sector']} stock; member sits on {names}.")

        # 3. That committee met close to the trade
        if flags.get("committee_timing") and meetings and overlap and tx:
            hits = [h for h in (meetings.nearest(c, tx, window) for c in overlap) if h]
            if hits:
                gap, ev = min(hits, key=lambda h: abs(h[0]))
                when = (f"{abs(gap)} days after" if gap > 0 else
                        f"{abs(gap)} days before" if gap < 0 else "the same day as")
                add_flag(t, "timing", "Committee timing",
                         f"Committee met {when} the trade: {ev['title'] or 'meeting'} ({ev['date']}).")

        # 4. What the stock did next, relative to SPY
        if flags.get("performance"):
            score_performance(t, cfg, market)
    return trades


def score_performance(t, cfg, market):
    """Score the trade against SPY. Returns True if a new Well-timed flag was added.

    The flag is only set once the full horizon has passed, so a trade isn't judged on a
    few days of price action. Until then the partial numbers are kept (marked incomplete)
    and the trade is rescored on later runs by refresh_performance().
    """
    tx = parse_date(t.get("transaction_date"))
    if not (market and t.get("ticker") and tx and t.get("type") in ("purchase", "sale")):
        return False
    if (t.get("perf") or {}).get("complete"):
        return False
    horizon_end = tx + timedelta(days=int(cfg["performance_horizon_days"]))
    end = min(horizon_end, date.today())
    if end <= tx:
        return False
    r, spy = market.forward_return(t["ticker"], tx, end), market.forward_return("SPY", tx, end)
    if r is None or spy is None:
        return False
    edge = (r - spy) if t["type"] == "purchase" else (spy - r)
    complete = end >= horizon_end
    t["perf"] = {"stock": round(r * 100, 1), "spy": round(spy * 100, 1),
                 "edge": round(edge * 100, 1), "through": str(end), "complete": complete}
    t.setdefault("flags", [])
    had_flag = any(f["code"] == "perf" for f in t["flags"])
    # Drop any flag set from a partial window (v3.0 did this); decide again on full data.
    t["flags"] = [f for f in t["flags"] if f["code"] != "perf"]
    if edge * 100 >= float(cfg["performance_flag_pct"]) and (complete or had_flag):
        verb = "rose" if r >= 0 else "fell"
        add_flag(t, "perf", "Well-timed",
                 f"After this {t['type']}, the stock {verb} {abs(r) * 100:.1f}% vs. SPY "
                 f"{spy * 100:+.1f}% through {end}.")
    return complete and not had_flag and any(f["code"] == "perf" for f in t["flags"])


def refresh_performance(store, cfg, market, skip_ids=()):
    """Rescore stored trades whose performance window hadn't finished when first seen."""
    if not (cfg["flags"].get("performance") and market):
        return []
    horizon = int(cfg["performance_horizon_days"])
    newly_flagged = []
    for t in store.values():
        if t["id"] in skip_ids or (t.get("perf") or {}).get("complete"):
            continue
        tx = parse_date(t.get("transaction_date"))
        # Old records from v3.0 were scored once with no "complete" marker. Recheck any
        # trade young enough that its first score may have been partial.
        if not tx or (date.today() - tx).days > horizon + 120:
            continue
        if score_performance(t, cfg, market):
            newly_flagged.append(t)
    if newly_flagged:
        log.info("%d earlier trades gained a Well-timed flag after their window closed",
                 len(newly_flagged))
    return newly_flagged


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

CSV_FIELDS = ["disclosure_date", "transaction_date", "chamber", "name", "state", "owner", "ticker",
              "asset", "type", "amount", "sector", "committees", "filing_lag_days", "flags",
              "flag_details", "perf_edge_pct", "perf_complete", "amended_dates", "link", "source", "id"]


def load_trades():
    if TRADES_PATH.exists():
        return json.loads(TRADES_PATH.read_text())
    return {}


def note_amendment(original, repeat):
    """Record a later filing of a trade already stored. The original disclosure date is
    kept, so the late-filing check measures the first disclosure, not the correction."""
    dates = set(original.get("amended_dates", []))
    if repeat.get("disclosure_date") and repeat["disclosure_date"] != original.get("disclosure_date"):
        dates.add(repeat["disclosure_date"])
    original["amended_dates"] = sorted(dates)
    links = original.setdefault("amended_links", [])
    if repeat.get("link") and repeat["link"] != original.get("link") and repeat["link"] not in links:
        links.append(repeat["link"])


def collapse_duplicates(store):
    """Fold together records saved before v3.1 that are the same trade filed twice."""
    groups = {}
    for t in store.values():
        k = core_key(t)
        if k:
            groups.setdefault(k, []).append(t)
    removed = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: (x.get("disclosure_date") or "9999", x.get("first_seen") or ""))
        keep = group[0]
        for dup in group[1:]:
            note_amendment(keep, dup)
            store.pop(dup["id"], None)
            removed += 1
    return removed


def save_trades(store):
    DATA_DIR.mkdir(exist_ok=True)
    SITE_DIR.mkdir(exist_ok=True)
    TRADES_PATH.write_text(json.dumps(store, indent=1))
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for t in sorted(store.values(), key=lambda x: x["disclosure_date"], reverse=True):
            w.writerow({
                **{k: t.get(k, "") for k in CSV_FIELDS},
                "committees": " ".join(t.get("committees", [])),
                "flags": " ".join(fl["code"] for fl in t.get("flags", [])),
                "flag_details": " | ".join(fl["detail"] for fl in t.get("flags", [])),
                "perf_edge_pct": (t.get("perf") or {}).get("edge", ""),
                "perf_complete": (t.get("perf") or {}).get("complete", ""),
                "amended_dates": " ".join(t.get("amended_dates", [])),
            })


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def describe(t):
    who = f"{t['name']} ({t['chamber'].title()}{', ' + t['state'] if t['state'] else ''})"
    what = f"{t['type']} {t['ticker'] or t['asset']} {t['amount']}".strip()
    owner = f" [{t['owner']}]" if t["owner"] not in ("self", "unknown") else ""
    flags = "".join(f"\n  - {fl['label']}: {fl['detail']}" for fl in t.get("flags", []))
    return f"{who}: {what}{owner} on {t['transaction_date'] or 'n/a'}{flags}"


def should_alert(t, mode):
    if mode == "all":
        return True
    if mode == "watchlist":
        return t.get("watched")
    return bool(t.get("flags")) or t.get("watched")


def send_alerts(new, cfg):
    picked = [t for t in new if should_alert(t, cfg["alert_on"])]
    if not picked:
        return
    body = "\n\n".join(describe(t) for t in picked[:25])
    if len(picked) > 25:
        body += f"\n\n...and {len(picked) - 25} more in the report"
    title = f"{APP}: {len(picked)} new trade{'s' if len(picked) != 1 else ''} to review"
    a = cfg["alerts"]
    if a.get("ntfy_topic"):
        try:
            headers = {"Title": title, "Tags": "star"}
            if cfg["report"].get("site_url"):
                headers["Click"] = cfg["report"]["site_url"]
            requests.post(f"https://ntfy.sh/{a['ntfy_topic']}", data=body[:3500].encode("utf-8"),
                          headers=headers, timeout=15)
        except Exception as e:
            log.error("ntfy push failed: %s", e)
    if a.get("desktop") and not os.environ.get("CI"):
        try:
            if platform.system() == "Darwin":
                subprocess.run(["osascript", "-e", f'display notification "{len(picked)} new" '
                                f'with title "{title}"'], check=False)
            elif platform.system() == "Linux":
                subprocess.run(["notify-send", title, body[:300]], check=False)
            else:
                from plyer import notification  # optional on Windows
                notification.notify(title=title, message=body[:250])
        except Exception as e:
            log.debug("Desktop alert failed: %s", e)
    if a.get("webhook_url"):
        try:
            text = f"**{title}**\n{body}"[:1900]
            requests.post(a["webhook_url"], json={"content": text, "text": text}, timeout=15)
        except Exception as e:
            log.error("Webhook failed: %s", e)
    e = a.get("email", {})
    if e.get("enabled") and e.get("to"):
        try:
            msg = MIMEText(body)
            msg["Subject"], msg["From"], msg["To"] = title, e["username"], e["to"]
            with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"])) as s:
                s.starttls()
                s.login(e["username"], e["password"])
                s.send_message(msg)
        except Exception as err:
            log.error("Email failed: %s", err)
    log.info("Alerted on %d trades", len(picked))


# ---------------------------------------------------------------------------
# Public report
# ---------------------------------------------------------------------------

FLAG_INFO = {
    "late": ("Late filing", "Disclosed after the STOCK Act's 45-day deadline. Objective and verifiable."),
    "committee": ("Committee oversight", "The member sits on a committee that oversees the company's sector."),
    "timing": ("Committee timing", "That committee met within a short window of the trade."),
    "perf": ("Well-timed", "The stock beat SPY by the set threshold in the direction of the trade."),
}

REPORT_CSS = """
:root{--paper:#EEF1F4;--sheet:#FFFFFF;--ink:#1D2433;--muted:#5B6475;--rule:#CFD6DE;
--late:#A35F0A;--committee:#1F6F78;--timing:#8E2C48;--perf:#3D5A99;--focus:#2B59C3}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#151A23;--sheet:#1D2430;
--ink:#E6EAF0;--muted:#A3ACBA;--rule:#323C4C;--late:#E3A64A;--committee:#5FBFC7;--timing:#E27A97;
--perf:#8FA8E8;--focus:#8FB0FF}}
:root[data-theme="dark"]{--paper:#151A23;--sheet:#1D2430;--ink:#E6EAF0;--muted:#A3ACBA;--rule:#323C4C;
--late:#E3A64A;--committee:#5FBFC7;--timing:#E27A97;--perf:#8FA8E8;--focus:#8FB0FF}
*{box-sizing:border-box}html{height:100%}
:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}
body{margin:0;background:var(--paper);color:var(--ink);
font:16px/1.6 "Public Sans",system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-variant-numeric:tabular-nums}
main{max-width:72rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}
.lede{max-width:46rem}
h1{font-size:clamp(2.4rem,7vw,4rem);line-height:1;font-weight:800;letter-spacing:-.03em;margin:0}
.lede .sub-title{font-size:1.3rem;color:var(--ink);font-weight:600;margin:.35rem 0 1.25rem}
.lede p{font-size:1.15rem;color:var(--muted);margin:.25rem 0}
.lede strong{color:var(--ink)}
h2{font-size:1.35rem;font-weight:700;margin:3rem 0 .75rem}
.key{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:1rem;margin:2rem 0 0;padding:0;list-style:none}
.key li{border-top:4px solid var(--c);padding:.75rem 0 0}
.key b{display:block;color:var(--c)}.key span{color:var(--muted);font-size:.92rem}
.controls{display:flex;flex-wrap:wrap;gap:.75rem;align-items:center;margin:1rem 0}
input[type=search]{flex:1 1 16rem;font:inherit;padding:.55rem .75rem;border:1px solid var(--rule);
border-radius:6px;background:var(--sheet);color:var(--ink)}
.chip{font:inherit;font-size:.9rem;padding:.35rem .8rem;border-radius:999px;border:1.5px solid var(--c,var(--rule));
background:transparent;color:var(--c,var(--ink));cursor:pointer}
.chip[aria-pressed=true]{background:var(--c,var(--ink));color:var(--sheet)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.table{overflow-x:auto;background:var(--sheet);border:1px solid var(--rule);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:52rem;font-size:.94rem}
th,td{text-align:left;padding:.65rem .8rem;border-bottom:1px solid var(--rule);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:.85rem;position:sticky;top:0;background:var(--sheet)}
tr:last-child td{border-bottom:0}
td:nth-child(6){min-width:20rem}
td .sub{display:block;color:var(--muted);font-size:.85rem}
.flag{display:block;border-left:3px solid var(--c);padding-left:.5rem;margin:.15rem 0;font-size:.88rem}
.flag b{color:var(--c);font-weight:600}
.buy{color:var(--committee)}.sell{color:var(--timing)}
.empty{padding:2rem;text-align:center;color:var(--muted)}
.method{max-width:46rem;color:var(--muted)}.method p,.method li{margin:.4rem 0}
.method strong{color:var(--ink)}
a{color:var(--focus)}
footer{margin-top:3rem;font-size:.85rem;color:var(--muted)}
"""

REPORT_JS = """
const rows=[...document.querySelectorAll('tbody tr[data-flags]')];
const q=document.getElementById('q'),chips=[...document.querySelectorAll('.chip')];
function apply(){const term=q.value.trim().toLowerCase();
const active=chips.filter(c=>c.getAttribute('aria-pressed')==='true').map(c=>c.dataset.f);
let shown=0;rows.forEach(r=>{const inFlagged=!!r.closest('#flagged');const okT=!term||r.dataset.text.includes(term);
const okF=!active.length||active.every(f=>r.dataset.flags.split(' ').includes(f));
const show=okT&&okF;r.hidden=!show;if(show&&inFlagged)shown++});
document.getElementById('count').textContent=shown+' flagged trade'+(shown===1?'':'s')+' shown';}
q.addEventListener('input',apply);
chips.forEach(c=>c.addEventListener('click',()=>{c.setAttribute('aria-pressed',
c.getAttribute('aria-pressed')==='true'?'false':'true');apply()}));apply();
"""


def esc(x):
    return html.escape(str(x if x is not None else ""))


def trade_row(t, committee_names):
    flags = t.get("flags", [])
    flag_html = "".join(
        f'<span class="flag" style="--c:var(--{f["code"] if f["code"] != "perf" else "perf"})">'
        f'<b>{esc(f["label"])}</b> {esc(f["detail"])}</span>' for f in flags) or '<span class="sub">None</span>'
    kind = "buy" if t["type"] == "purchase" else "sell" if t["type"] == "sale" else ""
    owner = "" if t["owner"] in ("self", "unknown") else f'<span class="sub">{esc(t["owner"])}</span>'
    link = f'<a href="{esc(t["link"])}" rel="noopener">Filing</a>' if t.get("link") else ""
    text = " ".join(str(t.get(k, "")) for k in ("name", "state", "ticker", "asset", "sector")).lower()
    return (f'<tr data-flags="{esc(" ".join(f["code"] for f in flags))}" data-text="{esc(text)}">'
            f'<td>{esc(t["name"])}<span class="sub">{esc(t["chamber"].title())}'
            f'{", " + esc(t["state"]) if t["state"] else ""}</span></td>'
            f'<td><b>{esc(t["ticker"] or "")}</b><span class="sub">{esc(t["asset"])[:80]}</span></td>'
            f'<td class="{kind}">{esc(t["type"].title())}{owner}</td>'
            f'<td>{esc(t["amount"])}</td>'
            f'<td>{esc(t["transaction_date"])}<span class="sub">filed {esc(t["disclosure_date"])}</span>'
            + (f'<span class="sub">amended {esc(", ".join(t["amended_dates"]))}</span>'
               if t.get("amended_dates") else "") + '</td>'
            f'<td>{flag_html}</td><td>{link}</td></tr>')


def table(trades, committee_names, empty_msg):
    head = ("<tr><th>Member</th><th>Asset</th><th>Type</th><th>Amount</th>"
            "<th>Traded</th><th>Flags</th><th>Source</th></tr>")
    body = "".join(trade_row(t, committee_names) for t in trades) or \
        f'<tr><td colspan="7" class="empty">{esc(empty_msg)}</td></tr>'
    return f'<div class="table"><table><thead>{head}</thead><tbody>{body}</tbody></table></div>'


MANIFEST = {
    "name": "Earendil", "short_name": "Earendil",
    "description": "Congressional stock trades, with accountability context.",
    "start_url": "./", "scope": "./", "display": "standalone",
    "background_color": "#EEF1F4", "theme_color": "#1D2433",
    "icons": [
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
        {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

# Network first, so the newest report always wins; falls back to the last copy offline.
SERVICE_WORKER = """const C='earendil-v1';
self.addEventListener('install',e=>self.skipWaiting());
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;
e.respondWith(fetch(e.request).then(r=>{const copy=r.clone();caches.open(C).then(c=>c.put(e.request,copy));return r})
.catch(()=>caches.match(e.request)));});
"""


def write_app_shell():
    SITE_DIR.mkdir(exist_ok=True)
    (SITE_DIR / "manifest.webmanifest").write_text(json.dumps(MANIFEST, indent=1))
    (SITE_DIR / "sw.js").write_text(SERVICE_WORKER)
    (SITE_DIR / ".nojekyll").write_text("")


def build_report(store, cfg, committee_names=None, demo=False):
    committee_names = committee_names or {}
    write_app_shell()
    trades = sorted(store.values(), key=lambda t: (t["disclosure_date"], t["transaction_date"]), reverse=True)
    flagged = [t for t in trades if t.get("flags")]
    late = sum(any(f["code"] == "late" for f in t.get("flags", [])) for t in trades)
    dates = [t["disclosure_date"] for t in trades if t["disclosure_date"]]
    since = min(dates) if dates else "n/a"
    rc = cfg["report"]
    focus = (rc.get("focus_state") or "").upper()
    focus_trades = [t for t in trades if focus and t.get("state") == focus]
    on = cfg["flags"]

    setting = {"late": "late_filing", "committee": "committee_sector",
               "timing": "committee_timing", "perf": "performance"}
    # Show a flag if it's switched on, or if saved trades still carry it from earlier runs.
    present = {f["code"] for t in trades for f in t.get("flags", [])}
    active = [(code, info) for code, info in FLAG_INFO.items() if on.get(setting[code]) or code in present]
    key_items = "".join(
        f'<li style="--c:var(--{code})"><b>{esc(name)}</b><span>{esc(desc)}</span></li>'
        for code, (name, desc) in active)
    chips = "".join(
        f'<button class="chip" style="--c:var(--{code})" data-f="{code}" aria-pressed="false">{esc(name)}</button>'
        for code, (name, _) in active)

    focus_html = ""
    if focus:
        focus_html = (f"<h2>{esc(focus)} delegation</h2>"
                      + table(focus_trades, committee_names,
                              f"No trades from {focus}'s delegation in this period."))

    lede_counts = (f"<p>Of <strong>{len(trades):,}</strong> trades disclosed since {esc(since)}, "
                   f"<strong>{len(flagged):,}</strong> raised at least one flag, including "
                   f"<strong>{late:,}</strong> filed past the legal deadline.</p>") if trades else \
        "<p>No trades collected yet. The next scheduled run will fill this page in.</p>"
    demo_note = ("<p><strong>Demo data.</strong> Every member and trade on this page is fictional, "
                 "generated to show how the report works.</p>") if demo else ""

    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{esc(rc['title'])}{" (demo)" if demo else ""}</title>
<meta name="description" content="{esc(rc.get('subtitle', ''))}">
<link rel="manifest" href="manifest.webmanifest"><meta name="theme-color" content="#1D2433">
<link rel="icon" href="icon.svg" type="image/svg+xml"><link rel="apple-touch-icon" href="icon-180.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Earendil">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>{REPORT_CSS}</style></head><body><main>
<header class="lede">
<h1>{esc(rc['title'])}</h1>
<p class="sub-title">{esc(rc.get('subtitle', ''))}</p>
{demo_note}
{lede_counts}
<p>A flag marks a pattern worth a closer look. It is not evidence of wrongdoing.</p>
<ul class="key">{key_items}</ul>
</header>
{focus_html}
<h2>Flagged trades</h2>
<div class="controls"><input id="q" type="search" placeholder="Search member, state, ticker, or sector"
aria-label="Search trades"><span id="count" aria-live="polite"></span></div>
<div class="controls" role="group" aria-label="Filter by flag">{chips}</div>
<div id="flagged">{table(flagged, committee_names, "No trades raised a flag in this period.")}</div>
<details><summary><h2 style="display:inline">All trades ({len(trades):,})</h2></summary>
{table(trades, committee_names, "No trades recorded yet. Run the tracker to collect filings.")}
</details>
<h2>How this works</h2>
<section class="method">
<p><strong>Source.</strong> Periodic transaction reports filed by members of Congress under the STOCK Act,
via {"fictional demo records" if demo else "Financial Modeling Prep's structured feed of House and Senate filings"}.
Each row links to the original filing where one is available.</p>
<p><strong>Late filing.</strong> The filing date minus the transaction date, compared with the
{int(cfg['late_filing_days'])}-day statutory deadline. Members sometimes learn of a spouse's or managed
account's trade later, and late fees are often paid quietly, so this measures timeliness, not intent.</p>
<p><strong>Committee oversight.</strong> Committee rosters come from the open
<a href="https://github.com/unitedstates/congress-legislators">congress-legislators</a> project; sectors come
from the company's market profile. Which committees oversee which sectors is an editorial judgment, published
in the source code. Broad committees like Appropriations are excluded because they touch everything.</p>
<p><strong>Committee timing.</strong> Meeting dates from the Congress.gov API, within
{int(cfg['timing_window_days'])} days of the trade. Committees meet often, so proximity alone proves little.</p>
<p><strong>Performance.</strong> Price change over {int(cfg['performance_horizon_days'])} days after the trade
compared with SPY. Disclosures report dollar ranges, not exact amounts, so dollar gains are not estimated.</p>
<p><strong>Limits.</strong> Name matching between filings and rosters can fail for members with common names;
unmatched members get no committee flags. Report errors and they will be corrected.</p>
</section>
<footer>{"" if demo else '<p><a href="trade_log.csv" download>Download every trade as a spreadsheet (CSV)</a></p>'}
<p>Updated {esc(datetime.now(timezone.utc).strftime('%B %d, %Y at %I:%M %p'))} UTC by {APP} v{VERSION}.</p></footer>
</main><script>{REPORT_JS}
if('serviceWorker' in navigator)navigator.serviceWorker.register('sw.js').catch(()=>{{}});</script></body></html>"""
    out = DEMO_PATH if demo else REPORT_PATH
    out.write_text(page)
    log.info("Report written to %s", out)


# ---------------------------------------------------------------------------
# Demo mode (fictional data only)
# ---------------------------------------------------------------------------

class DemoRoster:
    committee_names = {"HSAS": "House Committee on Armed Services",
                       "SSBK": "Senate Committee on Banking, Housing, and Urban Affairs",
                       "HSIF": "House Committee on Energy and Commerce"}
    people = {"D1": ("house", "Avery Holt", "ID", ["HSAS"]),
              "D2": ("senate", "Morgan Reyes", "OH", ["SSBK"]),
              "D3": ("house", "Jordan Pike", "TX", ["HSIF"])}

    def resolve(self, t):
        return next((b for b, p in self.people.items() if p[1] == t["name"]), None)

    def committees_for(self, b):
        return self.people[b][3]

    def state_for(self, b):
        return self.people[b][2]


class DemoMarket:
    SECTORS = {"LMT": "Industrials", "JPM": "Financial Services", "PFE": "Healthcare",
               "AAPL": "Technology", "NOC": "Industrials", "XOM": "Energy"}
    RETURNS = {"LMT": .14, "NOC": .03, "JPM": -.02, "PFE": .12, "AAPL": .04, "XOM": .01, "SPY": .015}

    def sector(self, ticker):
        return self.SECTORS.get(ticker, "")

    def forward_return(self, ticker, d0, d1):
        return self.RETURNS.get(ticker)


class DemoMeetings:
    def nearest(self, code, d, window):
        if code == "HSAS":
            return (-6, {"date": str(d + timedelta(days=-6)), "title": "Markup of the defense authorization bill"})
        return None


def demo_trades():
    today = date.today()

    def mk(chamber, name, state, ticker, asset, typ, amount, tx_ago, lag, owner="self"):
        tx = today - timedelta(days=tx_ago)
        low, high = parse_amount(amount)
        t = {"chamber": chamber, "first": name.split()[0], "last": name.split()[-1], "name": name,
             "state": state, "district": state, "owner": owner, "ticker": ticker, "asset": asset,
             "asset_type": "Stock", "type": typ, "amount": amount, "amount_low": low, "amount_high": high,
             "transaction_date": str(tx), "disclosure_date": str(tx + timedelta(days=lag)),
             "link": "", "source": "fmp"}
        t["id"] = trade_id(t)
        return t
    return [
        mk("house", "Avery Holt", "ID", "LMT", "Lockheed Martin Corp", "purchase", "$15,001 - $50,000", 70, 52),
        mk("house", "Avery Holt", "ID", "AAPL", "Apple Inc", "sale", "$1,001 - $15,000", 40, 20, "spouse"),
        mk("senate", "Morgan Reyes", "OH", "JPM", "JPMorgan Chase & Co", "purchase", "$50,001 - $100,000", 60, 30),
        mk("house", "Jordan Pike", "TX", "PFE", "Pfizer Inc", "purchase", "$100,001 - $250,000", 90, 61, "joint"),
        mk("house", "Jordan Pike", "TX", "XOM", "Exxon Mobil Corp", "sale", "$1,001 - $15,000", 30, 12),
        mk("senate", "Casey Lund", "WA", "NOC", "Northrop Grumman Corp", "purchase", "$1,001 - $15,000", 25, 14),
    ]


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(cfg, demo=False):
    store = load_trades() if not demo else {}
    if demo:
        raw, roster, market, meetings = demo_trades(), DemoRoster(), DemoMarket(), DemoMeetings()
        cfg = deep_merge(cfg, {"flags": {"late_filing": True, "committee_sector": True,
                                          "committee_timing": True, "performance": True}})
    else:
        raw = fetch_fmp(cfg) if cfg["data_source"] == "fmp" else fetch_portal(cfg)
        need_roster = cfg["flags"]["committee_sector"] or cfg["flags"]["committee_timing"]
        try:
            roster = Roster() if need_roster else None
        except Exception as e:
            log.error("Could not load committee rosters (%s); committee flags skipped.", e)
            roster = None
        market = Market(cfg) if cfg["fmp_api_key"] else None
        meetings = Meetings(cfg) if cfg["flags"]["committee_timing"] else None

    merged = collapse_duplicates(store)
    if merged:
        log.info("Merged %d duplicate records from amended filings already in the store", merged)

    trades = apply_filters(raw, cfg)
    by_core = {k: t for t in store.values() if (k := core_key(t))}
    new, amended = [], 0
    # Oldest disclosure first, so when an original and its amendment arrive together
    # the original is kept and the amendment is folded into it.
    for t in sorted(trades, key=lambda x: x["disclosure_date"] or "9999"):
        if t["id"] in store:
            continue
        k = core_key(t)
        if k and k in by_core:
            note_amendment(by_core[k], t)
            amended += 1
            continue
        new.append(t)
        store[t["id"]] = t
        if k:
            by_core[k] = t
    enrich(new, cfg, roster, market, meetings)
    for t in new:
        t["first_seen"] = str(date.today())
    rescored = refresh_performance(store, cfg, market, skip_ids={t["id"] for t in new})
    log.info("%d trades kept, %d new, %d new with flags, %d amended filings merged",
             len(trades), len(new), sum(bool(t["flags"]) for t in new), amended)

    if not demo:
        save_trades(store)
        if new or rescored:
            send_alerts(new + rescored, cfg)
    if cfg["report"]["enabled"]:
        build_report(store, cfg, getattr(roster, "committee_names", {}), demo=demo)
    return new


def main():
    ap = argparse.ArgumentParser(description="Earendil congressional trade tracker")
    ap.add_argument("--loop", type=int, metavar="MINUTES", help="run repeatedly every N minutes")
    ap.add_argument("--report", action="store_true", help="rebuild docs/index.html from saved trades")
    ap.add_argument("--demo", action="store_true", help="fictional sample data, writes docs/demo.html only")
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.demo:
        run_once(cfg, demo=True)
        return
    cfg = check_flag_keys(cfg)

    if args.report:
        names = {}
        try:
            names = Roster().committee_names
        except Exception:
            pass
        build_report(load_trades(), cfg, names)
        return
    while True:
        try:
            run_once(cfg)
        except Exception as e:
            log.error("Run failed: %s", e)
            if not args.loop:
                sys.exit(1)
        if not args.loop:
            break
        log.info("Sleeping %d minutes", args.loop)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
