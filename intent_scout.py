"""
intent_scout.py — Agent 6, "the Intent Scout".

What it does, in plain English:
  1. Reads your search phrases from the "IntentQueries" tab and your live
     campaign bundle-IDs from the "MyCampaigns" tab (both are yours to edit).
  2. Scrapes matching LinkedIn POSTS from the last 14 days using the Apify actor
     "harvestapi/linkedin-post-search" (no LinkedIn login, no cookies — Apify
     does the reading). It only READS public posts.
  3. Writes each new post to the "PostLeads" tab (deduped by post URL).
  4. Uses Claude (claude-opus-4-8) to (a) judge whether a post is a genuine
     traffic/budget/publisher hand-raiser or just noise (job ads, courses, book
     publishers, domain sales — skipped), and (b) draft a short public comment in
     Kawalpreet's voice for the genuine ones. YOU post those comments by hand.
  5. Feeds the genuine authors into the existing "Leads" tab (Source
     "intent-scout", Tier "A", DMApproval "pending") so Agent 3 can draft DMs —
     spending ZERO Apollo credits.
  6. Flags "PERFECT MATCH" posts — ones whose text mentions one of YOUR live
     campaign bundle-IDs — pins them to the top and pings you immediately.

It protects your Apify budget above all: a hard monthly cap (default $4.50 on the
$5 free plan) is checked BEFORE every run, and the run is skipped with a warning
if it might push you over. The counter resets each calendar month automatically.

CRITICAL SAFETY RULE — DO NOT VIOLATE:
  Agent 6 NEVER logs into LinkedIn, NEVER posts, comments, connects, or automates
  anything on LinkedIn. It only reads scraped public data (via Apify) and writes
  to your Google Sheet. Every comment is drafted for a HUMAN (Kawalpreet) to
  review and post manually.

Run a local test from the terminal, e.g.:
    python3 intent_scout.py            # full run, capped at SCOUT_MAX_POSTS
    python3 intent_scout.py 15         # full run capped at 15 posts (~$0.03)
"""

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import anthropic
import requests

import sheets
from sourcer import LEADS_HEADERS, LEADS_TAB

log = logging.getLogger("intent-scout")

# ---------------------------------------------------------------------------
# Model (same pattern as the other agents: prefer the latest, fall back if the
# exact string is ever rejected). claude-opus-4-8 is the latest, most capable.
# ---------------------------------------------------------------------------
PREFERRED_MODEL = "claude-opus-4-8"
FALLBACK_MODEL = "claude-opus-4-8"
active_model = PREFERRED_MODEL

_claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ---------------------------------------------------------------------------
# Apify actor + budget config (all env-driven, sensible defaults shown).
# ---------------------------------------------------------------------------
APIFY_ACTOR = "harvestapi~linkedin-post-search"     # '~' form for the API path
APIFY_BASE = "https://api.apify.com/v2"

# The $5/mo free plan: never spend more than this. Each scraped post ≈ $0.002.
DEFAULT_MONTHLY_BUDGET_USD = 4.50
DEFAULT_COST_PER_POST = 0.002
DEFAULT_MAX_POSTS = 125          # per weekly run, split evenly across queries
DEFAULT_POSTED_DAYS = 14         # only posts from the last N days


def monthly_budget():
    try:
        return float(os.environ.get("APIFY_MONTHLY_BUDGET_USD", DEFAULT_MONTHLY_BUDGET_USD))
    except (TypeError, ValueError):
        return DEFAULT_MONTHLY_BUDGET_USD


def cost_per_post():
    try:
        return float(os.environ.get("APIFY_COST_PER_POST", DEFAULT_COST_PER_POST))
    except (TypeError, ValueError):
        return DEFAULT_COST_PER_POST


def max_posts_per_run():
    try:
        return int(os.environ.get("SCOUT_MAX_POSTS", DEFAULT_MAX_POSTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_POSTS


def posted_days():
    try:
        return int(os.environ.get("SCOUT_POSTED_DAYS", DEFAULT_POSTED_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_POSTED_DAYS


# ---------------------------------------------------------------------------
# Tab names + headers
# ---------------------------------------------------------------------------
INTENT_QUERIES_TAB = "IntentQueries"
INTENT_QUERIES_HEADERS = ["Query", "Active", "Notes"]
# The real trade language you gave (you can edit this tab any time).
INTENT_QUERIES_SEED = [
    "looking budget", "looking for budget", "direct budget", "CPI direct",
    "CPA direct", "looking for publishers", "affiliates wanted",
    "need traffic for campaign",
]

MY_CAMPAIGNS_TAB = "MyCampaigns"
MY_CAMPAIGNS_HEADERS = ["CampaignName", "BundleID", "Vertical", "Payout/Notes", "Active"]

POST_LEADS_TAB = "PostLeads"
POST_LEADS_HEADERS = [
    "ScrapedDate", "PostURL", "PostDate", "PostText", "AuthorName",
    "AuthorHeadline", "AuthorProfileURL", "MatchedQuery", "DraftComment",
    "CommentStatus", "ConnectQueued", "PerfectMatch",
]

# Tiny key/value store shared with the Sourcer (credit/spend counters live here).
META_TAB = "Meta"
META_HEADERS = ["Key", "Value", "Updated"]

# The one column Agent 6 ADDS to the shared Leads tab (appended at the end so it
# never disturbs the Sourcer's / Personalizer's / Cockpit's existing columns).
LEADS_SOURCE_HEADER = "Source"
INTENT_SOURCE_VALUE = "intent-scout"

# Verticals we can truthfully mention in comments (never invented).
LIVE_VERTICALS = "BFSI, gaming, OTT and dating"


# ===========================================================================
# Sheet plumbing (all degrade gracefully when no Sheet is connected)
# ===========================================================================
def _ss():
    return sheets.open_spreadsheet()


def _tab(title, headers):
    ss = _ss()
    return sheets.ensure_tab(ss, title, headers) if ss else None


def _intent_ws():
    return _tab(INTENT_QUERIES_TAB, INTENT_QUERIES_HEADERS)


def _campaigns_ws():
    return _tab(MY_CAMPAIGNS_TAB, MY_CAMPAIGNS_HEADERS)


def _postleads_ws():
    return _tab(POST_LEADS_TAB, POST_LEADS_HEADERS)


def _meta_ws():
    return _tab(META_TAB, META_HEADERS)


def _leads_ws():
    return _tab(LEADS_TAB, LEADS_HEADERS)


def _col_letter(index_zero_based: int) -> str:
    """Turn a 0-based column number into a spreadsheet letter (0→A, 26→AA)."""
    letter, n = "", index_zero_based + 1
    while n:
        n, remainder = divmod(n - 1, 26)
        letter = chr(ord("A") + remainder) + letter
    return letter


# ---------------------------------------------------------------------------
# Meta counter (monthly Apify spend + last-run stats). Same idea as the
# Sourcer's credit counter: keyed by calendar month so it auto-resets.
# ---------------------------------------------------------------------------
def _meta_get(key, default=""):
    ws = _meta_ws()
    if not ws:
        return default
    for row in ws.get_all_values()[1:]:
        if row and row[0].strip().lower() == key.lower():
            return row[1] if len(row) > 1 else default
    return default


def _meta_set(key, value):
    ws = _meta_ws()
    if not ws:
        return
    sheets.upsert_row(ws, key_columns=[0], new_row=[key, str(value), sheets.now_utc()],
                      key_values=[key])


def _current_month():
    return sheets.now_utc()[:7]  # 'YYYY-MM'


def _spend_key(month):
    return f"apify_spend_{month}"


def spend_this_month(month=None):
    """Dollars of Apify spend recorded this calendar month."""
    month = month or _current_month()
    raw = _meta_get(_spend_key(month), "0")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _add_spend(month, dollars):
    new_total = round(spend_this_month(month) + dollars, 4)
    _meta_set(_spend_key(month), new_total)
    return new_total


# ---------------------------------------------------------------------------
# Reading your config tabs
# ---------------------------------------------------------------------------
def _is_yes(value):
    return str(value).strip().lower() in ("yes", "y", "true", "1", "active", "on")


def read_active_queries():
    """Active phrases from IntentQueries. Seeds the tab on first use."""
    ws = _intent_ws()
    if not ws:
        return []
    rows = ws.get_all_values()
    # First-time seeding: header only (or empty) → write the real trade phrases.
    if len(rows) <= 1:
        ws.append_rows([[q, "yes", ""] for q in INTENT_QUERIES_SEED])
        log.info("Seeded %s with %d phrases.", INTENT_QUERIES_TAB, len(INTENT_QUERIES_SEED))
        return list(INTENT_QUERIES_SEED)
    out = []
    for row in rows[1:]:
        query = (row[0] if len(row) > 0 else "").strip()
        active = row[1] if len(row) > 1 else ""
        if query and _is_yes(active):
            out.append(query)
    return out


def read_active_campaigns():
    """
    Active rows from MyCampaigns. Returns a list of dicts with keys
    name, bundle_id, vertical, notes. Rows with a blank BundleID are ignored
    (a bundle-id is what we search for and match on). The tab is created empty.
    """
    ws = _campaigns_ws()
    if not ws:
        return []
    out = []
    for row in ws.get_all_values()[1:]:
        name = (row[0] if len(row) > 0 else "").strip()
        bundle = (row[1] if len(row) > 1 else "").strip()
        vertical = (row[2] if len(row) > 2 else "").strip()
        notes = (row[3] if len(row) > 3 else "").strip()
        active = row[4] if len(row) > 4 else ""
        if bundle and _is_yes(active):
            out.append({"name": name or bundle, "bundle_id": bundle,
                        "vertical": vertical, "notes": notes})
    return out


# ===========================================================================
# Apify client (start run → wait → fetch dataset). Mirrors apollo.py's style:
# every failure raises ApifyError so the caller can post a clear message.
# ===========================================================================
class ApifyError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def _apify_token():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise ApifyError(0, "No APIFY_API_TOKEN is set in the environment / .env file.")
    return token


# How long to wait for a scrape run to finish before giving up (seconds).
APIFY_RUN_TIMEOUT = int(os.environ.get("APIFY_RUN_TIMEOUT", "1200"))
APIFY_POLL_SECONDS = int(os.environ.get("APIFY_POLL_SECONDS", "6"))
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


def run_actor(queries, per_query_max, since_date):
    """
    Start the actor for all queries at once, wait for it to finish, and return
    the scraped items (a list of post dicts). maxPosts is per-query. Raises
    ApifyError on any failure or timeout.
    """
    token = _apify_token()
    run_input = {
        "searchQueries": queries,
        "maxPosts": per_query_max,
        "sortBy": "date",                 # newest first
        "profileScraperMode": "short",    # cheapest author detail
        "postedLimitDate": since_date,    # exact 14-day cutoff (ISO date)
    }

    # 1) Start the run.
    try:
        resp = requests.post(
            f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs",
            params={"token": token}, json=run_input, timeout=60,
        )
    except requests.RequestException as exc:
        raise ApifyError(0, f"Could not reach Apify (network error): {exc}") from exc
    if resp.status_code not in (200, 201):
        raise ApifyError(resp.status_code,
                         f"Apify wouldn't start the run (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json().get("data", {})
    run_id = data.get("id")
    dataset_id = data.get("defaultDatasetId")
    if not run_id or not dataset_id:
        raise ApifyError(0, "Apify started the run but returned no run/dataset id.")

    # 2) Poll until it finishes (or we hit our timeout).
    waited = 0
    status = data.get("status", "")
    while status not in _TERMINAL and waited < APIFY_RUN_TIMEOUT:
        time.sleep(APIFY_POLL_SECONDS)
        waited += APIFY_POLL_SECONDS
        try:
            poll = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}",
                                params={"token": token}, timeout=30)
        except requests.RequestException:
            continue  # transient — try again next tick
        if poll.status_code == 200:
            status = poll.json().get("data", {}).get("status", status)

    if status != "SUCCEEDED":
        detail = status or f"still running after {APIFY_RUN_TIMEOUT}s"
        raise ApifyError(0, f"Scrape did not finish cleanly (status: {detail}).")

    # 3) Fetch the dataset items.
    try:
        items_resp = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"token": token, "clean": "true", "format": "json"}, timeout=120,
        )
    except requests.RequestException as exc:
        raise ApifyError(0, f"Scrape finished but the results couldn't be fetched: {exc}") from exc
    if items_resp.status_code != 200:
        raise ApifyError(items_resp.status_code,
                         f"Couldn't fetch scrape results (HTTP {items_resp.status_code}).")
    items = items_resp.json()
    return items if isinstance(items, list) else []


# ===========================================================================
# Parsing scraped posts (field names confirmed from a live test on 2026-07-04)
# ===========================================================================
def _clean_url(url):
    """Strip tracking query strings (e.g. ?miniProfileUrn=…) and trailing slash."""
    if not url:
        return ""
    url = str(url).split("?", 1)[0].strip()
    return url.rstrip("/")


def _first(d, *keys):
    """First present, non-empty value among dotted-path keys."""
    for key in keys:
        cur = d
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur:
            return cur
    return ""


def parse_post(item):
    """Turn one raw Apify item into the fields Agent 6 stores. Tolerant of shape."""
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    name = (
        _first(item, "authorName", "author.name", "author.fullName")
        or (f"{_first(author, 'firstName')} {_first(author, 'lastName')}".strip())
        or "(unknown)"
    )
    posted = _first(item, "postedAt.date", "postedAt", "postedDate", "date", "publishedAt")
    if isinstance(posted, dict):
        posted = posted.get("date", "")
    return {
        "PostURL": _clean_url(_first(item, "linkedinUrl", "postUrl", "url", "link")),
        "PostDate": str(posted or ""),
        "PostText": (_first(item, "content", "text", "postText", "description") or "")[:500],
        "AuthorName": name,
        "AuthorHeadline": _first(item, "author.info", "authorHeadline", "author.headline",
                                 "author.occupation"),
        "AuthorProfileURL": _clean_url(_first(item, "author.linkedinUrl", "authorProfileUrl",
                                              "author.profileUrl", "author.url")),
        "MatchedQuery": _first(item, "query.search") or "",
    }


def _within_window(post_date_iso, since_date):
    """
    True if the post's date is on/after the 14-day cutoff. Belt-and-suspenders on
    top of the actor's own date filter. Unknown/blank dates are KEPT (we don't
    throw away a post just because its date didn't parse).
    """
    if not post_date_iso:
        return True
    try:
        d = datetime.fromisoformat(post_date_iso.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        return True
    try:
        cutoff = datetime.fromisoformat(since_date).date()
    except (ValueError, AttributeError):
        return True
    return d >= cutoff


# ===========================================================================
# Relevance gate + comment drafting — ONE Claude call per new post.
# ===========================================================================
_RELEVANCE_SYSTEM = (
    "You screen scraped LinkedIn posts for Brand Shapers, a Delhi-NCR "
    "performance/affiliate marketing agency, and draft a short public comment "
    "for the good ones on behalf of Kawalpreet Singh, Head of Operations.\n\n"
    "STEP 1 — CLASSIFY the post:\n"
    "GENUINE (worth engaging) means either:\n"
    "  (a) Someone WITH TRAFFIC seeking budgets/offers/campaigns — bundle-id "
    "wish-lists, MMP language (Appsflyer, Adjust, onelink), hashtags like #CPA "
    "#CPI #MMP #Inapptraffic, phrases like 'HQ traffic', 'direct budget', "
    "'looking for offers'. Call this side = 'publisher'.\n"
    "  (b) A company/agency/network seeking PUBLISHERS, traffic, media buyers, or "
    "affiliates for their campaigns. Call this side = 'advertiser'.\n"
    "NOISE (skip) means: hiring/job posts, recruiters, book or content "
    "publishers, domain sales, courses/webinars/'DM me to learn', gurus, generic "
    "motivational content. Do NOT skip a post just because of its vertical — "
    "betting, casino and gambling posts are all in scope.\n"
    "There is NO geography filter — posts from anywhere are in scope.\n\n"
    "STEP 2 — IF GENUINE, DRAFT A COMMENT (2 to 4 sentences), in Kawalpreet's "
    "voice: a helpful peer, never salesy. Reference what THEIR post is about.\n"
    "  - For a 'publisher'-side post (they have traffic): say we have direct "
    f"budgets/campaigns live, quality offers across {LIVE_VERTICALS}, and "
    "reliable payouts — and that you'd be happy to connect.\n"
    "  - For an 'advertiser'-side post (they have campaigns/want publishers): say "
    "we run performance campaigns with fraud-screened, quality traffic — and "
    "that you'd be happy to connect.\n"
    "  - OFFER, don't ask. No 'let's hop on a call'. No invented numbers, no "
    "client names, no statistics, no links.\n"
    "  - A PERFECT-MATCH note may be supplied naming a specific campaign we have "
    "live and direct; if so you MAY truthfully say we have THAT campaign's budget "
    "live and direct and invite them to connect. NEVER name any campaign that is "
    "not explicitly supplied to you.\n"
    "  - VARIETY: you may be given a list of opening words already used in this "
    "batch — do NOT start your comment with any of them, and do not open with "
    "'Congrats' if it appears in that list.\n\n"
    "OUTPUT: respond with ONLY a JSON object, no other text:\n"
    '{"relevant": true/false, "side": "publisher"|"advertiser"|"", '
    '"reason": "<short>", "comment": "<the comment, or empty string if not relevant>"}'
)


def _extract_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _first_word(text):
    match = re.search(r"[A-Za-z']+", text or "")
    return match.group(0).lower() if match else ""


def screen_and_draft(post, perfect_campaign=None, used_openers=None):
    """
    One Claude call: classify the post and (if genuine) draft the comment.
    `perfect_campaign` is a dict {name, notes} when the post is a perfect match.
    `used_openers` is a set of opening words already used this run (for variety).
    Returns dict: {relevant, side, reason, comment}.
    """
    global active_model
    used_openers = used_openers or set()

    lines = [
        f"Author: {post['AuthorName']}"
        + (f" — {post['AuthorHeadline']}" if post["AuthorHeadline"] else ""),
        f"Matched search query: {post['MatchedQuery'] or '(unknown)'}",
    ]
    if perfect_campaign:
        note = perfect_campaign.get("name", "")
        if perfect_campaign.get("notes"):
            note += f" ({perfect_campaign['notes']})"
        lines.append(f"PERFECT MATCH — we have this campaign live and direct: {note}")
    if used_openers:
        lines.append("Opening words already used this run (do NOT start with these): "
                     + ", ".join(sorted(used_openers)))
    lines.append(f"\nTheir post:\n\"\"\"\n{post['PostText']}\n\"\"\"")
    user_prompt = "\n".join(lines)

    response = None
    for _ in range(2):  # one retry only, to swap the model if it's rejected
        try:
            response = _claude.messages.create(
                model=active_model, max_tokens=500,
                system=_RELEVANCE_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
            break
        except (anthropic.NotFoundError, anthropic.BadRequestError) as error:
            if active_model != FALLBACK_MODEL:
                log.warning("Model %r rejected (%s); falling back to %r.",
                            active_model, error.__class__.__name__, FALLBACK_MODEL)
                active_model = FALLBACK_MODEL
                continue
            raise

    text = "".join(b.text for b in response.content if b.type == "text")
    try:
        parsed = _extract_json(text)
    except json.JSONDecodeError:
        # If Claude didn't return clean JSON, fail safe: treat as noise (no comment).
        return {"relevant": False, "side": "", "reason": "unparseable model reply", "comment": ""}

    return {
        "relevant": bool(parsed.get("relevant")),
        "side": (parsed.get("side") or "").strip().lower(),
        "reason": (parsed.get("reason") or "").strip(),
        "comment": (parsed.get("comment") or "").strip(),
    }


# ===========================================================================
# Feeding genuine authors into the shared Leads tab (0 Apollo credits)
# ===========================================================================
def _ensure_source_column(leads_ws):
    """Append a 'Source' column to the Leads tab if it's missing. Returns header."""
    header = leads_ws.row_values(1)
    if LEADS_SOURCE_HEADER not in header:
        idx = len(header)  # 0-based index of the new column
        if leads_ws.col_count < idx + 1:
            leads_ws.add_cols(idx + 1 - leads_ws.col_count)
        leads_ws.update(range_name=f"{_col_letter(idx)}1", values=[[LEADS_SOURCE_HEADER]])
        header = header + [LEADS_SOURCE_HEADER]
        log.info("Added '%s' column to the Leads tab.", LEADS_SOURCE_HEADER)
    return header


def _existing_leads_urls(leads_ws, header):
    """Set of cleaned LinkedIn URLs already in the Leads tab (for dedupe)."""
    if "LinkedIn URL" not in header:
        return set()
    idx = header.index("LinkedIn URL")
    urls = set()
    for row in leads_ws.get_all_values()[1:]:
        if len(row) > idx and row[idx].strip():
            urls.add(_clean_url(row[idx]).lower())
    return urls


def _feed_author_to_leads(leads_ws, header, existing_urls, *, name, headline,
                          profile_url, segment, pipeline):
    """
    Add one minimal Lead row for an intent-scout author, deduped by LinkedIn URL.
    Returns True if a new row was added, False if already present or unusable.
    """
    clean = _clean_url(profile_url).lower()
    if not clean or clean in existing_urls:
        return False

    values = {
        "Name": name,
        "Title": headline,
        "LinkedIn URL": _clean_url(profile_url),
        "Segment": segment,
        "Pipeline": pipeline,
        "Tier": "A",
        "Status": "new",
        "DateAdded": sheets.now_utc(),
        LEADS_SOURCE_HEADER: INTENT_SOURCE_VALUE,
        "DMApproval": "pending",   # human approval gate (set by Agent 3 too)
    }
    row = [values.get(col_name, "") for col_name in header]
    leads_ws.append_row(row)
    existing_urls.add(clean)
    return True


# ===========================================================================
# THE CORE FUNCTION
# ===========================================================================
def run_scout(max_posts=None, notify=None):
    """
    Run one full Intent Scout pass. Returns a summary dict. If `notify` is given
    (a function taking one string) it receives the perfect-match pings AND the
    final plain-English summary — both destined for #outreach-control.
    """
    def announce(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                log.exception("Could not post Slack message.")

    max_posts = int(max_posts or max_posts_per_run())
    summary = {"ok": True}

    ss = _ss()
    if not ss:
        msg = "ℹ️ No Google Sheet connected — Intent Scout can't run."
        announce(msg)
        return {"ok": False, "reason": "no_sheet", "message": msg}

    # 1) Gather queries: IntentQueries phrases + Active MyCampaigns bundle-ids.
    phrases = read_active_queries()
    campaigns = read_active_campaigns()
    bundle_ids = [c["bundle_id"] for c in campaigns]
    # bundle-id (lowercased) → campaign dict, for perfect-match naming.
    bundle_map = {c["bundle_id"].lower(): c for c in campaigns}
    queries = phrases + bundle_ids
    if not queries:
        msg = ("⚠️ Intent Scout has no active queries to run. Add phrases to the "
               f"'{INTENT_QUERIES_TAB}' tab (Active = yes).")
        announce(msg)
        return {"ok": False, "reason": "no_queries", "message": msg}

    # 2) Budget guard — check BEFORE spending anything.
    month = _current_month()
    spent = spend_this_month(month)
    budget = monthly_budget()
    ppp = cost_per_post()
    n = len(queries)
    per_query = max(1, math.ceil(max_posts / n))
    run_max_cost = per_query * n * ppp
    if spent + run_max_cost > budget:
        msg = (f"🛑 *Intent Scout skipped — budget guard.*\n"
               f"• This month's Apify spend: *${spent:.2f}* of *${budget:.2f}*\n"
               f"• This run could cost up to *${run_max_cost:.2f}* "
               f"({per_query}×{n} queries), which would exceed the cap.\n"
               f"• No scrape was run. The cap resets on the 1st.")
        announce(msg)
        return {"ok": True, "skipped": True, "reason": "budget", "spent": spent,
                "budget": budget, "would_cost": run_max_cost}

    # 3) Scrape (last N days, newest first).
    since_date = (datetime.now(timezone.utc) - timedelta(days=posted_days())).strftime("%Y-%m-%d")
    try:
        items = run_actor(queries, per_query, since_date)
    except ApifyError as err:
        msg = f"❌ Intent Scout scrape failed: {err.message}"
        announce(msg)
        return {"ok": False, "reason": "apify_error", "message": err.message}

    scraped = len(items)
    spend_now = round(scraped * ppp, 4)
    spent_after = _add_spend(month, spend_now) if scraped else spent

    # 4) Parse, keep only the 14-day window, dedupe against PostLeads by PostURL.
    post_ws = _postleads_ws()
    existing_post_urls = set()
    for row in (post_ws.get_all_values()[1:] if post_ws else []):
        if len(row) > 1 and row[1].strip():
            existing_post_urls.add(_clean_url(row[1]).lower())

    parsed = []
    for item in items:
        p = parse_post(item)
        if not p["PostURL"]:
            continue
        if p["PostURL"].lower() in existing_post_urls:
            continue  # already in PostLeads from a previous run
        if not _within_window(p["PostDate"], since_date):
            continue
        existing_post_urls.add(p["PostURL"].lower())  # dedupe within this run too
        # Perfect match: post text mentions one of our active bundle-ids.
        text_l = p["PostText"].lower()
        matched = next((bundle_map[b] for b in bundle_map if b and b in text_l), None)
        p["_perfect"] = matched  # dict or None
        parsed.append(p)

    # Perfect matches first, then trim to the post cap for this run.
    parsed.sort(key=lambda x: 0 if x["_perfect"] else 1)
    new_posts = parsed[:max_posts]

    # 5) Prepare the Leads tab once (append Source column, load dedupe set).
    leads_ws = _leads_ws()
    leads_header = _ensure_source_column(leads_ws) if leads_ws else []
    leads_urls = _existing_leads_urls(leads_ws, leads_header) if leads_ws else set()

    # 6) Screen + draft each new post; feed genuine authors to Leads.
    scraped_date = sheets.now_utc()
    used_openers = set()
    rows_to_write, genuine, skipped, perfect_count, fed = [], 0, 0, 0, 0

    for p in new_posts:
        perfect = p["_perfect"]
        result = screen_and_draft(p, perfect_campaign=perfect, used_openers=used_openers)

        connect_queued = ""
        if result["relevant"] and result["comment"]:
            genuine += 1
            comment = result["comment"]
            status = "ready"
            opener = _first_word(comment)
            if opener:
                used_openers.add(opener)
            # Pipeline inference from which side of the market they're on.
            pipeline = "advertiser" if result["side"] == "advertiser" else "publisher"
            seg_hint = perfect["bundle_id"] if perfect else (p["MatchedQuery"] or "post")
            if leads_ws and _feed_author_to_leads(
                leads_ws, leads_header, leads_urls,
                name=p["AuthorName"], headline=p["AuthorHeadline"],
                profile_url=p["AuthorProfileURL"], segment=f"intent: {seg_hint}",
                pipeline=pipeline,
            ):
                fed += 1
            connect_queued = "yes"
        else:
            skipped += 1
            comment = ""
            status = "skip — not relevant"

        if perfect:
            perfect_count += 1
            announce(f"🔥 PERFECT MATCH: {p['AuthorName']} is asking for "
                     f"{perfect['name']} — row added to PostLeads.")

        rows_to_write.append([
            scraped_date, p["PostURL"], p["PostDate"], p["PostText"],
            p["AuthorName"], p["AuthorHeadline"], p["AuthorProfileURL"],
            p["MatchedQuery"], comment, status, connect_queued,
            "yes" if perfect else "",
        ])

    # 7) Write the new rows to PostLeads (perfect matches already on top).
    if rows_to_write and post_ws:
        post_ws.append_rows(rows_to_write, value_input_option="RAW")

    # 8) Record run stats for `scout status`.
    counts = (f"scraped={scraped} new={len(new_posts)} genuine={genuine} "
              f"skipped={skipped} leads={fed} perfect={perfect_count}")
    _meta_set("scout_last_run", sheets.now_utc())
    _meta_set("scout_last_counts", counts)

    # 9) Post the run summary.
    campaign_note = (f"{len(bundle_ids)} campaign queries"
                     if bundle_ids else "0 campaign queries")
    msg = (
        f"🛰️ *Intent Scout run complete*\n"
        f"• Queries: *{len(phrases)}* phrases + {campaign_note}\n"
        f"• Posts scraped: *{scraped}* (spend this run ≈ *${spend_now:.2f}*)\n"
        f"• New posts added to PostLeads: *{len(new_posts)}*\n"
        f"• Comments drafted (genuine): *{genuine}*  ·  skipped as noise: *{skipped}*\n"
        f"• 🔥 Perfect matches: *{perfect_count}*\n"
        f"• Authors fed to Leads (Tier A, pending): *{fed}*  (0 Apollo credits)\n"
        f"• Apify spend this month: *${spent_after:.2f} / ${budget:.2f}*"
    )
    announce(msg)

    summary.update({
        "queries_phrases": len(phrases), "queries_campaigns": len(bundle_ids),
        "scraped": scraped, "new_posts": len(new_posts), "genuine": genuine,
        "skipped": skipped, "perfect": perfect_count, "fed_to_leads": fed,
        "spend_this_run": spend_now, "spend_month": spent_after, "budget": budget,
    })
    return summary


def scout_status(notify=None):
    """Report budget used, last run, and last counts. Returns a dict."""
    month = _current_month()
    spent = spend_this_month(month)
    budget = monthly_budget()
    last_run = _meta_get("scout_last_run", "(never)")
    last_counts = _meta_get("scout_last_counts", "(no runs yet)")
    phrases = read_active_queries()
    campaigns = read_active_campaigns()

    pct = (spent / budget * 100) if budget else 0
    msg = (
        f"🛰️ *Intent Scout — status*\n"
        f"• Apify spend this month: *${spent:.2f} / ${budget:.2f}* ({pct:.0f}%)\n"
        f"• Active queries: *{len(phrases)}* phrases + *{len(campaigns)}* campaign bundle-ids\n"
        f"• Last run: *{last_run}*\n"
        f"• Last run counts: {last_counts}"
    )
    if notify:
        notify(msg)
    else:
        log.info(msg)
    return {"ok": True, "spent": spent, "budget": budget, "message": msg,
            "phrases": len(phrases), "campaigns": len(campaigns),
            "last_run": last_run, "last_counts": last_counts}


# ---------------------------------------------------------------------------
# Local test from the terminal, e.g.:
#     python3 intent_scout.py          # full run (SCOUT_MAX_POSTS cap)
#     python3 intent_scout.py 15       # full run capped at 15 posts (~$0.03)
# This DOES scrape (small Apify spend) and DOES write to PostLeads/Leads.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else max_posts_per_run()
    print(f"Intent Scout test — cap {cap} posts.\n")
    run_scout(max_posts=cap, notify=print)
