"""
intent_scout.py — Agent 6, "the Intent Scout" (CAMPAIGN-DRIVEN).

What it does, in plain English:
  1. Reads your live campaigns from the "CampaignBriefs" tab of a SEPARATE
     Google Sheet (env CAMPAIGN_SHEET_ID). You add campaigns by pasting a raw
     brief in Slack ("add campaign …"); Claude parses it into fields.
  2. For each active campaign it builds up to 5 targeted searches — the exact
     CampaignName, CampaignName + "offer", CampaignName + "direct", plus its
     Android bundle IDs and iOS App Store IDs (a campaign can have several of
     each) — and scrapes matching LinkedIn POSTS from the last 14 days using the
     Apify actor "harvestapi/linkedin-post-search"
     (no LinkedIn login, no cookies — Apify does the reading). Reads only.
  3. Writes each new post to the "PublisherLeads" tab of that same campaign
     sheet (deduped by post URL).
  4. Uses Claude (claude-opus-4-8), in ONE call per post, to: (a) judge whether
     the author is a genuine publisher/media-buyer/affiliate (vs noise — job ads,
     courses, book publishers, domain sales); (b) classify DIRECTION — 'seeking'
     (asking for the campaign/budgets) vs 'offering' (a competitor announcing they
     already have it); (c) record a short Relevance note; and (d) for 'seeking'
     posts ONLY, draft a short public comment in Kawalpreet's voice that leads
     with our availability of THAT campaign. YOU post those comments by hand.
  5. Feeds ONLY the 'seeking' authors into the MAIN sheet's "Leads" tab (Source
     "intent-scout", Tier "A", DMApproval "pending", Pipeline "publisher",
     Segment "campaign: <name>") so Agent 3 can draft DMs — ZERO Apollo credits.
     'Offering'/competitor posts get no comment, no lead, no ping.
  6. Flags "PERFECT MATCH" posts — 'seeking' posts whose text names one of your
     campaigns' store IDs or CampaignName — sorts them up and pings you by name.

It protects your Apify budget above all: a hard monthly cap (default $4.50 on the
$5 free plan) is checked BEFORE every run, and the run is skipped with a warning
if it might push you over. The counter resets each calendar month automatically.

CRITICAL SAFETY RULE — DO NOT VIOLATE:
  Agent 6 NEVER logs into LinkedIn, NEVER posts, comments, connects, or automates
  anything on LinkedIn. It only reads scraped public data (via Apify) and writes
  to your Google Sheets. Every comment is drafted for a HUMAN (Kawalpreet) to
  review and post manually.

Run a local test from the terminal, e.g.:
    python3 intent_scout.py            # hunt ALL active campaigns (default cap)
    python3 intent_scout.py 15         # hunt all active campaigns, cap 15 posts
    python3 intent_scout.py 15 "Rocket Reels"   # hunt ONE campaign, cap 15
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
# DORMANT (kept, not deleted): the old phrase-hunting tab on the MAIN sheet. We
# no longer read or seed it — hunting is now driven by CampaignBriefs. Left here
# only so the constant/tab isn't silently forgotten.
INTENT_QUERIES_TAB = "IntentQueries"

# --- The SEPARATE campaign sheet (env CAMPAIGN_SHEET_ID) has these two tabs. ---
# A campaign can have SEVERAL Android bundle IDs and SEVERAL iOS App Store IDs
# (comma-separated in each cell) — the same offer often ships under more than one
# store listing.
CAMPAIGN_BRIEFS_TAB = "CampaignBriefs"
CAMPAIGN_BRIEFS_HEADERS = [
    "CampaignName", "AndroidBundleIDs", "iOSAppIDs", "Vertical", "Geos", "Model",
    "Payouts", "MMP", "RawBrief", "Active", "AddedDate",
]

PUBLISHER_LEADS_TAB = "PublisherLeads"
PUBLISHER_LEADS_HEADERS = [
    "ScrapedDate", "Campaign", "PostURL", "PostDate", "PostText", "AuthorName",
    "AuthorHeadline", "AuthorProfileURL", "MatchedQuery", "Direction",
    "PerfectMatch", "Relevance", "DraftComment", "CommentStatus", "ConnectQueued",
]

# Tiny key/value store shared with the Sourcer (credit/spend counters live here).
# It stays on the MAIN sheet, so the Apify spend counter is in one place.
META_TAB = "Meta"
META_HEADERS = ["Key", "Value", "Updated"]

# The one column Agent 6 ADDS to the shared Leads tab (appended at the end so it
# never disturbs the Sourcer's / Personalizer's / Cockpit's existing columns).
LEADS_SOURCE_HEADER = "Source"
INTENT_SOURCE_VALUE = "intent-scout"

# Max targeted searches per campaign: name, name+"offer", name+"direct", then
# each store ID — capped at this many.
MAX_QUERIES_PER_CAMPAIGN = 5


# ===========================================================================
# Sheet plumbing (all degrade gracefully when no Sheet is connected)
# ===========================================================================
# --- MAIN sheet (source of truth: Leads + the shared Meta/spend counter) ---
def _ss():
    return sheets.open_spreadsheet()


def _tab(title, headers):
    ss = _ss()
    return sheets.ensure_tab(ss, title, headers) if ss else None


def _meta_ws():
    return _tab(META_TAB, META_HEADERS)


def _leads_ws():
    return _tab(LEADS_TAB, LEADS_HEADERS)


# --- CAMPAIGN sheet (env CAMPAIGN_SHEET_ID: CampaignBriefs + PublisherLeads) ---
def _campaign_ss():
    return sheets.open_campaign_spreadsheet()


def _campaign_tab(title, headers):
    ss = _campaign_ss()
    return sheets.ensure_tab(ss, title, headers) if ss else None


# One-time-per-process guard so we only check/migrate the briefs header once.
_briefs_header_checked = False
# The old single-BundleID header used this column name; its presence flags a tab
# that predates the multi-ID (AndroidBundleIDs | iOSAppIDs) schema.
_OLD_BRIEFS_MARKER = "BundleID"


def _heal_briefs_header(ws):
    """
    If the CampaignBriefs tab still has the OLD single-BundleID header, rewrite it
    to the new multi-ID schema and clear the stale data rows (their columns are in
    the old layout and can't be auto-mapped safely — the operator re-pastes each
    brief, which repopulates them correctly). No-op if the header already matches.
    """
    try:
        current = ws.row_values(1)
    except Exception:
        return
    if current == CAMPAIGN_BRIEFS_HEADERS or _OLD_BRIEFS_MARKER not in current:
        return
    last = _col_letter(len(CAMPAIGN_BRIEFS_HEADERS) - 1)
    ws.update(range_name=f"A1:{last}1", values=[CAMPAIGN_BRIEFS_HEADERS])
    row_count = len(ws.get_all_values())
    if row_count > 1:
        ws.batch_clear([f"A2:{last}{row_count}"])
    log.info("Migrated CampaignBriefs to the multi-ID schema; cleared %d stale row(s).",
             max(0, row_count - 1))


def _briefs_ws():
    global _briefs_header_checked
    ws = _campaign_tab(CAMPAIGN_BRIEFS_TAB, CAMPAIGN_BRIEFS_HEADERS)
    if ws and not _briefs_header_checked:
        _heal_briefs_header(ws)
        _briefs_header_checked = True
    return ws


_publeads_header_checked = False


def _heal_publeads_header(ws):
    """
    If the PublisherLeads tab predates the new 'Direction' column, rewrite the
    header and clear the stale rows (old rows have no Direction value and their
    later columns are shifted one place vs the new labels). No-op once migrated.
    """
    try:
        current = ws.row_values(1)
    except Exception:
        return
    if current == PUBLISHER_LEADS_HEADERS:
        return
    if "Direction" in current or "PostURL" not in current:
        return  # already has Direction, or a layout we shouldn't touch
    last = _col_letter(len(PUBLISHER_LEADS_HEADERS) - 1)
    ws.update(range_name=f"A1:{last}1", values=[PUBLISHER_LEADS_HEADERS])
    row_count = len(ws.get_all_values())
    if row_count > 1:
        ws.batch_clear([f"A2:{last}{row_count}"])
    log.info("Migrated PublisherLeads to add Direction; cleared %d stale row(s).",
             max(0, row_count - 1))


def _publeads_ws():
    global _publeads_header_checked
    ws = _campaign_tab(PUBLISHER_LEADS_TAB, PUBLISHER_LEADS_HEADERS)
    if ws and not _publeads_header_checked:
        _heal_publeads_header(ws)
        _publeads_header_checked = True
    return ws


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


def reconcile_spend(month=None, set_counter=True):
    """
    Pull this actor's REAL Apify usage (usageTotalUsd) for `month` from the Apify
    API and, when set_counter is True, overwrite the Meta spend counter with that
    authoritative total — closing any gap from runs that bypassed the counter
    (e.g. early manual tests). Returns a dict: {month, actual, previous, runs}.
    """
    month = month or _current_month()
    token = _apify_token()
    try:
        resp = requests.get(f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs",
                            params={"token": token, "desc": "true", "limit": 1000},
                            timeout=60)
        runs = resp.json().get("data", {}).get("items", []) if resp.status_code == 200 else []
    except requests.RequestException as exc:
        raise ApifyError(0, f"Couldn't reach Apify to reconcile spend: {exc}") from exc

    total, n_runs = 0.0, 0
    for r in runs:
        if (r.get("startedAt") or "")[:7] != month:
            continue
        n_runs += 1
        try:
            total += float(r.get("usageTotalUsd") or 0)
        except (TypeError, ValueError):
            pass
    total = round(total, 4)
    previous = spend_this_month(month)
    if set_counter:
        _meta_set(_spend_key(month), total)
    return {"month": month, "actual": total, "previous": previous, "runs": n_runs}


# ---------------------------------------------------------------------------
# Reading + writing the CampaignBriefs tab
# ---------------------------------------------------------------------------
def _is_yes(value):
    return str(value).strip().lower() in ("yes", "y", "true", "1", "active", "on")


def _split_ids(cell_value):
    """Turn a comma-separated ID cell into a clean list (order preserved)."""
    return [x.strip() for x in str(cell_value or "").split(",") if x.strip()]


def _dedupe_ci(items):
    """De-duplicate a list case-insensitively, keeping first occurrence + order."""
    seen, out = set(), []
    for it in items:
        cleaned = str(it).strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return out


def read_active_campaigns():
    """
    Active rows from the CampaignBriefs tab (campaign sheet). Returns a list of
    dicts: name, android_ids (list), ios_ids (list), vertical, geos, model,
    payouts, mmp. A campaign needs at least a CampaignName to be huntable.
    """
    ws = _briefs_ws()
    if not ws:
        return []
    out = []
    for row in ws.get_all_values()[1:]:
        def cell(i):
            return (row[i].strip() if len(row) > i else "")
        name = cell(0)
        active = cell(9)   # Active is column J now (after the two ID columns)
        if name and _is_yes(active):
            out.append({
                "name": name,
                "android_ids": _split_ids(cell(1)),
                "ios_ids": _split_ids(cell(2)),
                "vertical": cell(3), "geos": cell(4), "model": cell(5),
                "payouts": cell(6), "mmp": cell(7),
            })
    return out


# --- Pulling EVERY store id out of the store links in a pasted brief ----------
# Google Play:  ...details?id=com.alphaware.reelShort   → com.alphaware.reelShort
# Apple App Store: ...apps.apple.com/us/app/foo/id123456 → id123456 (numeric)
# A brief may contain several of each, so we collect them ALL.
_PLAY_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_.]+)")
_APPLE_ID_RE = re.compile(r"/id(\d+)")


def _extract_store_ids(text):
    """
    Return (android_ids, ios_ids) — every Play Store package id and every Apple
    App Store numeric id (as 'idNNNN') found in the text, de-duplicated.
    """
    if not text:
        return [], []
    android = _dedupe_ci(_PLAY_ID_RE.findall(text))
    ios = _dedupe_ci(f"id{n}" for n in _APPLE_ID_RE.findall(text))
    return android, ios


_BRIEF_PARSE_SYSTEM = (
    "You extract structured fields from a raw affiliate/performance-marketing "
    "campaign brief pasted by a Brand Shapers operator. The brief may be in any "
    "format — a paragraph, bullet points, a forwarded message. Pull out what is "
    "actually present; never invent a value. (App bundle IDs and store links are "
    "handled separately — you do NOT need to extract those.)\n\n"
    "Fields:\n"
    "  - CampaignName: the campaign / app / offer name.\n"
    "  - Vertical: e.g. shortdrama, gaming, dating, OTT, BFSI, casino.\n"
    "  - Geos: target countries/regions (e.g. 'IN, GCC, US'). Empty if none.\n"
    "  - Model: pricing/conversion model (e.g. CPA, CPI, CPL, subscription, "
    "revshare). Empty if none.\n"
    "  - Payouts: payout amounts/terms exactly as stated (e.g. '$2.5 CPA, $40 "
    "CPS'). Empty if none.\n"
    "  - MMP: the mobile measurement partner if named. Capture WHATEVER value is "
    "labelled 'MMP' (or clearly is the tracking/attribution partner), even if the "
    "brand is unfamiliar to you — e.g. Appsflyer, Adjust, Singular, Kochava, "
    "Apptrove, Trackier. Empty only if none is stated.\n\n"
    "OUTPUT only a JSON object, no other text:\n"
    '{"CampaignName": "", "Vertical": "", "Geos": "", "Model": "", '
    '"Payouts": "", "MMP": ""}'
)


def parse_campaign_brief(raw_brief):
    """
    One Claude call for the semantic fields (name/vertical/geos/model/payouts/mmp)
    PLUS a deterministic regex pass that pulls EVERY store id out of the brief.
    Returns a dict with those fields plus android_ids / ios_ids (lists).
    """
    global active_model
    response = None
    for _ in range(2):  # one retry only, to swap the model if it's rejected
        try:
            response = _claude.messages.create(
                model=active_model, max_tokens=500,
                system=_BRIEF_PARSE_SYSTEM,
                messages=[{"role": "user", "content": raw_brief}],
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
        parsed = {}

    def field(key):
        return str(parsed.get(key, "") or "").strip()

    fields = {k: field(k) for k in
              ("CampaignName", "Vertical", "Geos", "Model", "Payouts", "MMP")}
    # Store IDs are extracted deterministically from the links, not by the model.
    fields["android_ids"], fields["ios_ids"] = _extract_store_ids(raw_brief)
    return fields


def _find_campaign_row(ws, name):
    """
    Return the existing CampaignBriefs row for `name` as a field dict (with
    android_ids/ios_ids lists), or None if there's no such row yet. Used so a
    re-paste MERGES store IDs instead of overwriting them.
    """
    want = name.strip().lower()
    for row in ws.get_all_values()[1:]:
        if row and row[0].strip().lower() == want:
            def cell(i):
                return (row[i].strip() if len(row) > i else "")
            return {
                "android_ids": _split_ids(cell(1)),
                "ios_ids": _split_ids(cell(2)),
                "Vertical": cell(3), "Geos": cell(4), "Model": cell(5),
                "Payouts": cell(6), "MMP": cell(7),
            }
    return None


def add_campaign(raw_brief, notify=None):
    """
    Parse a pasted brief and save it to CampaignBriefs with Active=yes (upsert,
    keyed by CampaignName). On a re-paste it MERGES newly-found Android/iOS store
    IDs into the existing lists (never loses any) and refreshes the other fields.
    Returns a dict summary. The parsed fields are also posted via `notify` if given.
    """
    ws = _briefs_ws()
    if not ws:
        return {"ok": False, "reason": "no_campaign_sheet",
                "message": ("No campaign sheet connected — set CAMPAIGN_SHEET_ID "
                            "(and share that sheet with the service account).")}

    fields = parse_campaign_brief(raw_brief)
    name = fields["CampaignName"]
    if not name:
        return {"ok": False, "reason": "no_name",
                "message": ("I couldn't find a campaign name in that brief. "
                            "Please include the campaign/app name and re-paste.")}

    existing = _find_campaign_row(ws, name)
    android = fields["android_ids"]
    ios = fields["ios_ids"]
    if existing:
        # Merge: keep every existing ID, add any new ones (never lose one).
        android = _dedupe_ci(existing["android_ids"] + android)
        ios = _dedupe_ci(existing["ios_ids"] + ios)

        # For the text fields, a new value wins; otherwise keep what we had.
        def keep(key):
            return fields.get(key) or existing.get(key, "")
        merged = {k: keep(k) for k in ("Vertical", "Geos", "Model", "Payouts", "MMP")}
    else:
        merged = {k: fields[k] for k in ("Vertical", "Geos", "Model", "Payouts", "MMP")}

    new_row = [
        name, ", ".join(android), ", ".join(ios), merged["Vertical"],
        merged["Geos"], merged["Model"], merged["Payouts"], merged["MMP"],
        raw_brief.strip()[:2000], "yes", sheets.now_utc(),
    ]
    updated = sheets.upsert_row(ws, key_columns=[0], new_row=new_row, key_values=[name])

    android_line = ", ".join(android) if android else "(none found)"
    ios_line = ", ".join(ios) if ios else "(none found)"
    if not android and not ios:
        android_line = ios_line = "(none — will hunt by campaign name only)"
    summary_msg = (
        f"✅ Campaign {'updated' if updated else 'added'} (Active): *{name}*\n"
        f"• Android bundle IDs: {android_line}\n"
        f"• iOS App Store IDs: {ios_line}\n"
        f"• Vertical: {merged['Vertical'] or '—'}\n"
        f"• Geos: {merged['Geos'] or '—'}\n"
        f"• Model: {merged['Model'] or '—'}\n"
        f"• Payouts: {merged['Payouts'] or '—'}  _(kept private — never shown in public comments)_\n"
        f"• MMP: {merged['MMP'] or '—'}  _(used for matching + DMs only — never in public comments)_\n"
        f"Saved to CampaignBriefs. Re-paste anytime to add more store links — existing IDs are kept."
    )
    if notify:
        try:
            notify(summary_msg)
        except Exception:
            log.exception("Could not post the campaign summary.")

    return {"ok": True, "updated": updated, "fields": fields, "name": name,
            "android_ids": android, "ios_ids": ios, "message": summary_msg}


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
        raise ApifyError(0, "APIFY_API_TOKEN is not visible to this process. Check the "
                            "'Env check at boot' log line — if it lists APIFY_API_TOKEN as "
                            "absent, the running container has a stale env; redeploy/restart "
                            "so it picks up the current variable.")
    return token


# How long to wait for a scrape run to finish before giving up (seconds).
APIFY_RUN_TIMEOUT = int(os.environ.get("APIFY_RUN_TIMEOUT", "1200"))
APIFY_POLL_SECONDS = int(os.environ.get("APIFY_POLL_SECONDS", "6"))
_TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "TIMED_OUT"}


def run_actor(queries, per_query_max, since_date):
    """
    Start the actor for all queries at once, wait for it to finish, and return
    (items, actual_cost_usd): the scraped post dicts plus the run's REAL Apify
    cost (usageTotalUsd) so the spend counter records truth, not an estimate.
    actual_cost_usd is None if Apify didn't report it. maxPosts is per-query.
    Raises ApifyError on any failure or timeout.
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

    # 2) Poll until it finishes (or we hit our timeout). Keep the final run data
    #    so we can read the real cost (usageTotalUsd) once it's SUCCEEDED.
    waited = 0
    status = data.get("status", "")
    run_data = data
    while status not in _TERMINAL and waited < APIFY_RUN_TIMEOUT:
        time.sleep(APIFY_POLL_SECONDS)
        waited += APIFY_POLL_SECONDS
        try:
            poll = requests.get(f"{APIFY_BASE}/actor-runs/{run_id}",
                                params={"token": token}, timeout=30)
        except requests.RequestException:
            continue  # transient — try again next tick
        if poll.status_code == 200:
            run_data = poll.json().get("data", {}) or run_data
            status = run_data.get("status", status)

    if status != "SUCCEEDED":
        detail = status or f"still running after {APIFY_RUN_TIMEOUT}s"
        raise ApifyError(0, f"Scrape did not finish cleanly (status: {detail}).")

    # The finished run carries its true billed cost. Fall back to None so the
    # caller can use its per-post estimate if Apify ever omits it.
    try:
        actual_cost = float(run_data.get("usageTotalUsd"))
    except (TypeError, ValueError):
        actual_cost = None

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
    return (items if isinstance(items, list) else []), actual_cost


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
    "You screen scraped LinkedIn posts for Brand Shapers, a performance/affiliate "
    "marketing agency, and draft a short public comment for the right ones on "
    "behalf of Kawalpreet Singh, Head of Operations. You are hunting for "
    "PUBLISHERS who want to RUN a specific live campaign whose details are "
    "supplied.\n\n"
    "STEP 1 — RELEVANT? "
    "GENUINE = the author is a publisher, media buyer, affiliate, or ad network "
    "active in performance/affiliate marketing. Signals: MMP language (Appsflyer, "
    "Adjust, Singular, Kochava, Apptrove, onelink), hashtags like #CPA #CPI #CPL "
    "#MMP #Inapptraffic, app bundle IDs or store links, phrases like 'HQ traffic', "
    "'direct budget', 'looking for offers', 'we have offers'. In scope regardless "
    "of vertical (betting, casino, gambling included) and regardless of the "
    "author's country — publishers are GLOBAL.\n"
    "NOISE (skip) = hiring/job posts, recruiters, book or content publishers, "
    "domain sales, courses/webinars/'DM me to learn', gurus, generic motivational "
    "content.\n\n"
    "STEP 2 — DIRECTION (only if genuine): classify the post as exactly one of:\n"
    "  • 'seeking' = the author is ASKING FOR the campaign, offers, or budgets to "
    "run on their traffic. E.g. 'looking this', 'looking for', 'need direct "
    "budget', 'who has', 'anyone have', a wish-list of bundle IDs, or a store link "
    "posted with looking/need language. MANY posters are non-native English "
    "speakers — judge by INTENT, not grammar.\n"
    "  • 'offering' = the author is ANNOUNCING they HAVE or RUN the campaign / "
    "offers / budgets to give out. E.g. 'we have it direct', 'offers available', "
    "'we are running', promo posts. These are competitors or suppliers, NOT "
    "prospects.\n\n"
    "STEP 3 — RELEVANCE NOTE (only if genuine): a SHORT note (a few words) on why "
    "they fit THIS campaign. BOOST when their post mentions the campaign's geos, "
    "model (CPA/subscription/etc.), vertical, or MMP. Example: 'IN+GCC traffic, "
    "CPA subs, uses Apptrove'. Empty string if genuine but nothing specific "
    "stands out.\n\n"
    "STEP 4 — COMMENT: draft one ONLY IF genuine AND direction is 'seeking'. For "
    "'offering' posts return an EMPTY comment. The comment is 2 to 3 sentences in "
    "Kawalpreet's voice — a helpful peer, never salesy:\n"
    "  - LEAD with availability: open by saying we run THIS campaign (the supplied "
    "one) live and direct.\n"
    "  - Then ONE line of fit using the campaign's geos and/or vertical.\n"
    "  - Then invite them to connect. OFFER, don't ask — no 'let's hop on a call'.\n"
    "  - NEVER mention the MMP / tracking partner in the comment.\n"
    "  - NEVER state payouts, rates, or any numbers. No client names, no "
    "statistics, no links.\n"
    "  - NEVER name any campaign other than the one supplied.\n"
    "  - VARIETY: you may be given a list of opening words already used in this "
    "batch — do NOT start your comment with any of them, and do not open with "
    "'Congrats' if it appears in that list.\n\n"
    "OUTPUT: respond with ONLY a JSON object, no other text:\n"
    '{"relevant": true/false, "direction": "seeking"|"offering"|"", '
    '"relevance": "<short note or empty>", '
    '"comment": "<the comment, or empty string if not a seeking match>"}'
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


def screen_and_draft(post, campaign, is_perfect=False, used_openers=None):
    """
    One Claude call: classify the post (relevant? seeking vs offering), write a
    relevance note, and — only for genuine 'seeking' posts — draft the comment, in
    the context of a specific `campaign` dict (name/geos/model/vertical/mmp).
    Payouts are deliberately NOT passed (never in a public comment); the MMP is
    passed for the relevance note but the prompt forbids it in the comment.
    `used_openers` is a set of opening words already used this run (for variety).
    Returns dict: {relevant, direction, relevance, comment}.
    """
    global active_model
    used_openers = used_openers or set()
    campaign = campaign or {}

    lines = [
        f"Campaign: {campaign.get('name', '(unknown)')}",
        f"Geos: {campaign.get('geos') or '—'}",
        f"Model: {campaign.get('model') or '—'}",
        f"Vertical: {campaign.get('vertical') or '—'}",
        f"MMP: {campaign.get('mmp') or '—'}",
    ]
    if is_perfect:
        lines.append("PERFECT MATCH — their post explicitly names this campaign or its app; "
                     "you may reference that directly.")
    lines += [
        f"\nAuthor: {post['AuthorName']}"
        + (f" — {post['AuthorHeadline']}" if post["AuthorHeadline"] else ""),
        f"Matched search query: {post['MatchedQuery'] or '(unknown)'}",
    ]
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
        return {"relevant": False, "direction": "", "relevance": "", "comment": ""}

    return {
        "relevant": bool(parsed.get("relevant")),
        "direction": (parsed.get("direction") or "").strip().lower(),
        "relevance": (parsed.get("relevance") or "").strip(),
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
# Query building + perfect-match detection (campaign-driven)
# ===========================================================================
def build_queries(campaigns):
    """
    For each campaign build up to MAX_QUERIES_PER_CAMPAIGN (5) targeted searches,
    in priority order: the exact CampaignName, CampaignName + "offer", CampaignName
    + "direct", then each Android bundle ID, then each iOS App Store ID (as
    'idNNNN'). The two context words ("offer"/"direct") pull affiliate hand-raisers
    that a bare, generic name would miss; store IDs catch bundle-ID wishlist posts.
    Returns (query_strings, query_map) where query_map maps each lowercased query
    string back to its campaign dict, so a scraped post's MatchedQuery tells us
    which campaign it belongs to.
    """
    query_strings, query_map = [], {}
    for c in campaigns:
        candidates = []
        if c["name"]:
            candidates.append(c["name"])
            candidates.append(f'{c["name"]} offer')
            candidates.append(f'{c["name"]} direct')
        candidates.extend(c["android_ids"])
        candidates.extend(c["ios_ids"])
        for q in candidates[:MAX_QUERIES_PER_CAMPAIGN]:
            key = q.strip().lower()
            if key and key not in query_map:
                query_map[key] = c
                query_strings.append(q)
    return query_strings, query_map


def _perfect_campaign_for(text_lower, campaigns):
    """
    Return the campaign whose CampaignName or ANY of its store IDs (Android or
    iOS) is named in the post text (a structural PERFECT MATCH), or None. Names
    shorter than 4 chars are ignored to avoid accidental substring hits.
    """
    for c in campaigns:
        for store_id in c["android_ids"] + c["ios_ids"]:
            if store_id and store_id.lower() in text_lower:
                return c
        name = c["name"].lower()
        if name and len(name) >= 4 and name in text_lower:
            return c
    return None


# ===========================================================================
# THE CORE FUNCTION
# ===========================================================================
def run_scout(max_posts=None, notify=None, campaign_name=None):
    """
    Run one Intent Scout pass. With no `campaign_name` it hunts ALL active
    campaigns; with one it hunts just that campaign (matched case-insensitively).
    Returns a summary dict. If `notify` is given (a function taking one string) it
    receives the perfect-match pings AND the final summary — for #outreach-control.
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

    # The campaign sheet holds the briefs + the PublisherLeads output.
    if not _campaign_ss():
        msg = ("ℹ️ No campaign sheet connected — set CAMPAIGN_SHEET_ID (and share "
               "that sheet with the service account). Intent Scout can't run.")
        announce(msg)
        return {"ok": False, "reason": "no_campaign_sheet", "message": msg}

    # 1) Gather active campaigns (optionally narrowed to one).
    campaigns = read_active_campaigns()
    if campaign_name:
        want = campaign_name.strip().lower()
        campaigns = [c for c in campaigns if c["name"].strip().lower() == want]
        if not campaigns:
            msg = (f"⚠️ No active campaign named '{campaign_name}' in CampaignBriefs. "
                   f"Add it first with 'add campaign …', or check the name.")
            announce(msg)
            return {"ok": False, "reason": "no_such_campaign", "message": msg}
    if not campaigns:
        msg = ("⚠️ Intent Scout has no active campaigns to hunt. Add one by pasting "
               "a brief: 'add campaign …'.")
        announce(msg)
        return {"ok": False, "reason": "no_campaigns", "message": msg}

    query_strings, query_map = build_queries(campaigns)
    if not query_strings:
        msg = "⚠️ Active campaigns have no usable name/bundle to search on."
        announce(msg)
        return {"ok": False, "reason": "no_queries", "message": msg}

    # 2) Budget guard — check BEFORE spending anything. The post cap is split
    #    evenly across ALL queries of all active campaigns.
    month = _current_month()
    spent = spend_this_month(month)
    budget = monthly_budget()
    ppp = cost_per_post()
    n = len(query_strings)
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
        items, actual_cost = run_actor(query_strings, per_query, since_date)
    except ApifyError as err:
        msg = f"❌ Intent Scout scrape failed: {err.message}"
        announce(msg)
        return {"ok": False, "reason": "apify_error", "message": err.message}

    scraped = len(items)
    # Record the REAL billed cost when Apify reports it; otherwise fall back to
    # the per-post estimate. This keeps the monthly counter drift-free.
    if actual_cost is not None:
        spend_now = round(actual_cost, 4)
    else:
        spend_now = round(scraped * ppp, 4)
    spent_after = _add_spend(month, spend_now) if spend_now else spent

    # 4) Parse, keep only the N-day window, dedupe against PublisherLeads by URL.
    post_ws = _publeads_ws()
    existing_post_urls = set()
    for row in (post_ws.get_all_values()[1:] if post_ws else []):
        if len(row) > 2 and row[2].strip():   # PostURL is column C in PublisherLeads
            existing_post_urls.add(_clean_url(row[2]).lower())

    only_campaign = campaigns[0] if len(campaigns) == 1 else None
    parsed = []
    for item in items:
        p = parse_post(item)
        if not p["PostURL"]:
            continue
        if p["PostURL"].lower() in existing_post_urls:
            continue  # already in PublisherLeads from a previous run
        if not _within_window(p["PostDate"], since_date):
            continue
        existing_post_urls.add(p["PostURL"].lower())  # dedupe within this run too
        # Which campaign is this post for? Perfect match (post NAMES a campaign)
        # wins; otherwise fall back to the campaign that owns the matched query.
        text_l = p["PostText"].lower()
        perfect = _perfect_campaign_for(text_l, campaigns)
        by_query = query_map.get(p["MatchedQuery"].strip().lower())
        p["_campaign"] = perfect or by_query or only_campaign
        p["_perfect"] = perfect
        parsed.append(p)

    # Perfect matches first, then trim to the post cap for this run.
    parsed.sort(key=lambda x: 0 if x["_perfect"] else 1)
    new_posts = parsed[:max_posts]

    # 5) Prepare the MAIN sheet's Leads tab once (Source column + dedupe set).
    leads_ws = _leads_ws()
    leads_header = _ensure_source_column(leads_ws) if leads_ws else []
    leads_urls = _existing_leads_urls(leads_ws, leads_header) if leads_ws else set()

    # 6) Screen + draft each new post; feed genuine authors to Leads. Per-campaign
    #    tallies are keyed by campaign name for the summary.
    scraped_date = sheets.now_utc()
    used_openers = set()
    rows_to_write = []
    genuine, offering, skipped, perfect_count, fed = 0, 0, 0, 0, 0
    per_campaign = {}   # name -> {posts, seeking, offering, perfect, fed}

    def tally(name, key):
        stats = per_campaign.setdefault(
            name, {"posts": 0, "seeking": 0, "offering": 0, "perfect": 0, "fed": 0})
        stats[key] += 1

    for p in new_posts:
        camp = p["_campaign"] or {}
        camp_name = camp.get("name", "(unassigned)")
        struct_perfect = p["_perfect"]   # post structurally names the campaign
        tally(camp_name, "posts")

        result = screen_and_draft(p, camp, is_perfect=bool(struct_perfect),
                                  used_openers=used_openers)
        direction = result["direction"]
        relevance = result["relevance"]

        connect_queued = ""
        is_perfect = False   # only SEEKING posts count as actionable perfect matches
        if result["relevant"] and direction == "seeking" and result["comment"]:
            genuine += 1
            tally(camp_name, "seeking")
            comment = result["comment"]
            status = "ready"
            opener = _first_word(comment)
            if opener:
                used_openers.add(opener)
            # Campaign hunts are always publisher-side.
            if leads_ws and _feed_author_to_leads(
                leads_ws, leads_header, leads_urls,
                name=p["AuthorName"], headline=p["AuthorHeadline"],
                profile_url=p["AuthorProfileURL"],
                segment=f"campaign: {camp_name}", pipeline="publisher",
            ):
                fed += 1
                tally(camp_name, "fed")
            connect_queued = "yes"
            if struct_perfect:
                is_perfect = True
                perfect_count += 1
                tally(camp_name, "perfect")
                announce(f"🔥 PERFECT MATCH: {p['AuthorName']} is asking for "
                         f"*{camp_name}* — row added to PublisherLeads.")
        elif result["relevant"] and direction == "offering":
            offering += 1
            tally(camp_name, "offering")
            comment = ""
            status = "skip — offering/competitor"
        else:
            skipped += 1
            comment = ""
            status = "skip — not relevant"

        rows_to_write.append([
            scraped_date, camp_name, p["PostURL"], p["PostDate"], p["PostText"],
            p["AuthorName"], p["AuthorHeadline"], p["AuthorProfileURL"],
            p["MatchedQuery"], direction, "yes" if is_perfect else "", relevance,
            comment, status, connect_queued,
        ])

    # 7) Write the new rows to PublisherLeads (perfect matches already on top).
    if rows_to_write and post_ws:
        post_ws.append_rows(rows_to_write, value_input_option="RAW")

    # 8) Record run stats for `scout status`.
    counts = (f"scraped={scraped} new={len(new_posts)} seeking={genuine} "
              f"offering={offering} noise={skipped} leads={fed} perfect={perfect_count}")
    _meta_set("scout_last_run", sheets.now_utc())
    _meta_set("scout_last_counts", counts)

    # 9) Post the run summary — per-campaign breakdown + monthly spend.
    if per_campaign:
        lines = []
        for name in sorted(per_campaign):
            s = per_campaign[name]
            lines.append(f"   – *{name}*: {s['posts']} posts · {s['seeking']} seeking · "
                         f"{s['offering']} offering · 🔥 {s['perfect']} perfect · "
                         f"{s['fed']} leads fed")
        per_campaign_block = "\n".join(lines)
    else:
        per_campaign_block = "   – (no new posts)"

    scope = f"1 campaign ('{campaigns[0]['name']}')" if campaign_name else f"{len(campaigns)} campaigns"
    msg = (
        f"🛰️ *Intent Scout run complete* — {scope}, {n} queries\n"
        f"• Posts scraped: *{scraped}* (spend this run ≈ *${spend_now:.2f}*)\n"
        f"• New posts added to PublisherLeads: *{len(new_posts)}*\n"
        f"• Seeking publishers (commented): *{genuine}*  ·  offering/competitors skipped: "
        f"*{offering}*  ·  noise skipped: *{skipped}*\n"
        f"• 🔥 Perfect matches: *{perfect_count}*  ·  Authors fed to Leads: *{fed}* (0 Apollo credits)\n"
        f"*Per campaign:*\n{per_campaign_block}\n"
        f"• Apify spend this month: *${spent_after:.2f} / ${budget:.2f}*"
    )
    announce(msg)

    summary.update({
        "campaigns": len(campaigns), "queries": n, "scraped": scraped,
        "new_posts": len(new_posts), "genuine": genuine, "seeking": genuine,
        "offering": offering, "skipped": skipped, "perfect": perfect_count,
        "fed_to_leads": fed, "per_campaign": per_campaign,
        "spend_this_run": spend_now, "spend_month": spent_after, "budget": budget,
    })
    return summary


def scout_status(notify=None):
    """Report budget used, last run, last counts, and the active campaigns."""
    month = _current_month()
    spent = spend_this_month(month)
    budget = monthly_budget()
    last_run = _meta_get("scout_last_run", "(never)")
    last_counts = _meta_get("scout_last_counts", "(no runs yet)")
    campaigns = read_active_campaigns()

    if campaigns:
        camp_lines = "\n".join(
            f"   – *{c['name']}*"
            + (f" ({c['geos']})" if c["geos"] else "")
            + (f" · {c['model']}" if c["model"] else "")
            for c in campaigns
        )
    else:
        camp_lines = "   – (none — add one with 'add campaign …')"

    pct = (spent / budget * 100) if budget else 0
    msg = (
        f"🛰️ *Intent Scout — status*\n"
        f"• Apify spend this month: *${spent:.2f} / ${budget:.2f}* ({pct:.0f}%)\n"
        f"• Active campaigns: *{len(campaigns)}*\n{camp_lines}\n"
        f"• Last run: *{last_run}*\n"
        f"• Last run counts: {last_counts}"
    )
    if notify:
        notify(msg)
    else:
        log.info(msg)
    return {"ok": True, "spent": spent, "budget": budget, "message": msg,
            "campaigns": len(campaigns), "last_run": last_run, "last_counts": last_counts}


# ---------------------------------------------------------------------------
# Local test from the terminal, e.g.:
#     python3 intent_scout.py                     # hunt all campaigns (default cap)
#     python3 intent_scout.py 15                  # hunt all campaigns, cap 15 (~$0.03)
#     python3 intent_scout.py 15 "Rocket Reels"   # hunt ONE campaign, cap 15
# This DOES scrape (small Apify spend) and DOES write to PublisherLeads/Leads.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
    cap = int(sys.argv[1]) if len(sys.argv) > 1 else max_posts_per_run()
    one_campaign = sys.argv[2] if len(sys.argv) > 2 else None
    scope = f" — campaign '{one_campaign}'" if one_campaign else ""
    print(f"Intent Scout test — cap {cap} posts{scope}.\n")
    run_scout(max_posts=cap, notify=print, campaign_name=one_campaign)
