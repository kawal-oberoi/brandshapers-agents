"""
sourcer.py — Agent 2, "the Sourcer".

What it does, in plain English:
  1. Runs a FREE Apollo People Search for a named segment (e.g. bfsi-lending).
  2. Scores every candidate A / B / C for free, using only the free search data,
     and throws away competitors and weak (C) matches.
  3. Enriches ONLY the best Tier-A-then-B keepers (each enrich ≈ 1 credit), up to
     a per-run limit (default 10) AND a per-MONTH credit cap (default 70).
  4. Writes the enriched keepers into the "Leads" tab with Status = "new".
  5. Skips the whole run if Agent 1 has marked that segment "paused".
  6. Never double-processes anyone (dedupes on the Apollo person id).

It protects your credits above all: search is free, dry runs cost nothing, and
the monthly cap stops the bot before it can overspend.

Edit SEARCH_PRESETS and the keyword lists below to tune targeting — they are
written to be readable and safe to change.
"""

import logging
import os

import apollo
import sheets

log = logging.getLogger("sourcer")

# ---------------------------------------------------------------------------
# Tab names + headers
# ---------------------------------------------------------------------------
LEADS_TAB = "Leads"
LEADS_HEADERS = [
    "Name", "Title", "Company", "LinkedIn URL", "Email", "Email Status", "Flag",
    "Location", "Segment", "Pipeline", "Tier", "Status", "ApolloID", "DateAdded",
]
META_TAB = "Meta"           # tiny key/value store for the credit counter, rotation, etc.
META_HEADERS = ["Key", "Value", "Updated"]
SEGMENTS_TAB = "Segments"   # owned by Agent 1; we only READ it to honour "paused".

# ---------------------------------------------------------------------------
# Credit guard
# ---------------------------------------------------------------------------
# Default cap on Apollo enrichment credits per calendar month. Override with the
# APOLLO_MONTHLY_CAP environment variable.
DEFAULT_MONTHLY_CAP = 70

# ---------------------------------------------------------------------------
# How wide to search (all FREE). 3 pages x 30 = up to 90 candidates per run.
# ---------------------------------------------------------------------------
SEARCH_POOL_PAGES = 3
SEARCH_PER_PAGE = 30

# Where summaries are posted.
OUTREACH_CHANNEL = "#outreach-control"

# ---------------------------------------------------------------------------
# SEARCH PRESETS — one per segment. Easy to edit.
#   pipeline               advertiser or publisher
#   person_titles          job titles to search (Apollo person_titles)
#   person_locations       always India for now
#   organization_keywords  employer keywords that define the segment
# ---------------------------------------------------------------------------
ADVERTISER_TITLES = [
    "User Acquisition Manager", "Growth Manager", "Growth Marketing Manager",
    "Performance Marketing Manager", "Head of Digital", "Head of Digital Marketing",
    "CMO", "Affiliate Manager", "Partnerships Manager",
]
PUBLISHER_TITLES = [
    "Affiliate Manager", "Media Buyer", "Monetization Manager", "Founder",
]
INDIA = ["India"]

SEARCH_PRESETS = {
    # ---- ADVERTISER pipeline (brands that want users/customers) ----
    "bfsi-insurance": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["insurance", "insurtech"],
    },
    "bfsi-lending": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["lending", "loans", "credit"],
    },
    "bfsi-neobank": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["neobank", "banking", "fintech"],
    },
    "bfsi-stocktrading": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["stock trading", "trading", "broking", "investing"],
    },
    "mobile-gaming": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["mobile gaming", "gaming", "games"],
    },
    "ott": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["ott", "streaming", "video on demand"],
    },
    "dating": {
        "pipeline": "advertiser",
        "person_titles": ADVERTISER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["dating", "matchmaking"],
    },
    # ---- PUBLISHER pipeline (ad networks / publishers we partner with) ----
    "publisher": {
        "pipeline": "publisher",
        "person_titles": PUBLISHER_TITLES,
        "person_locations": INDIA,
        "organization_keywords": ["ad network", "affiliate network", "publisher", "media"],
    },
}

# Friendly aliases so the brain can map loose phrasing to a preset key.
SEGMENT_ALIASES = {
    "insurance": "bfsi-insurance",
    "lending": "bfsi-lending",
    "loans": "bfsi-lending",
    "neobank": "bfsi-neobank",
    "banking": "bfsi-neobank",
    "stock trading": "bfsi-stocktrading",
    "stocktrading": "bfsi-stocktrading",
    "trading": "bfsi-stocktrading",
    "gaming": "mobile-gaming",
    "mobile gaming": "mobile-gaming",
    "streaming": "ott",
    "publishers": "publisher",
    "publisher leads": "publisher",
}

ADVERTISER_SEGMENTS = [k for k, v in SEARCH_PRESETS.items() if v["pipeline"] == "advertiser"]

# ---------------------------------------------------------------------------
# Scoring keywords — readable and safe to edit.
# ---------------------------------------------------------------------------
# Company-name hints that this is a COMPETITOR (another performance/affiliate/
# media MARKETING AGENCY). We never store these. Ad networks/publishers (names
# with "network"/"media") are deliberately NOT here, so the publisher pipeline
# can still target them.
COMPETITOR_NAME_KEYWORDS = [
    "performance marketing", "digital marketing agency", "marketing agency",
    "ad agency", "advertising agency", "growth agency", "affiliate agency",
    "ppc agency", "seo agency", "marketing services", "media agency",
    "marketing solutions",
]

# Title keywords that count as a "weaker but relevant" match (Tier B/C signal).
TITLE_KEYWORDS = {
    "advertiser": [
        "growth", "performance", "user acquisition", "acquisition", "digital marketing",
        "head of digital", "cmo", "chief marketing", "affiliate", "partnership",
        "marketing manager", "marketing",
    ],
    "publisher": [
        "affiliate", "media buy", "media buyer", "monetization", "monetisation",
        "founder", "co-founder", "publisher", "ad network",
    ],
}


# ---------------------------------------------------------------------------
# Sheet helpers (all degrade gracefully when no Sheet is connected)
# ---------------------------------------------------------------------------
def _leads_ws():
    ss = sheets.open_spreadsheet()
    return sheets.ensure_tab(ss, LEADS_TAB, LEADS_HEADERS) if ss else None


def _meta_ws():
    ss = sheets.open_spreadsheet()
    return sheets.ensure_tab(ss, META_TAB, META_HEADERS) if ss else None


def _segments_ws():
    ss = sheets.open_spreadsheet()
    if not ss:
        return None
    try:
        return ss.worksheet(SEGMENTS_TAB)
    except Exception:
        return None  # Agent 1 hasn't created it yet — treat everything as active.


def existing_apollo_ids():
    """Set of Apollo person ids already saved in the Leads tab (for dedupe)."""
    ws = _leads_ws()
    if not ws:
        return set()
    rows = ws.get_all_values()
    if not rows:
        return set()
    try:
        id_col = LEADS_HEADERS.index("ApolloID")
    except ValueError:
        return set()
    return {
        row[id_col].strip()
        for row in rows[1:]
        if len(row) > id_col and row[id_col].strip()
    }


def is_segment_paused(segment, pipeline):
    """
    True if Agent 1 has marked this segment paused/discontinued in the Segments
    tab for this pipeline (or 'both'). Matches segment names loosely so
    'mobile-gaming' and 'mobile gaming' line up. Defaults to NOT paused.
    """
    ws = _segments_ws()
    if not ws:
        return False
    want = segment.replace("-", " ").replace("_", " ").strip().lower()
    for row in ws.get_all_values()[1:]:
        if len(row) < 3:
            continue
        seg = row[0].replace("-", " ").replace("_", " ").strip().lower()
        pipe = row[1].strip().lower()
        status = row[2].strip().lower()
        if seg == want and pipe in (pipeline.lower(), "both"):
            if "paus" in status or "discontinu" in status or "off" == status:
                return True
    return False


# ---------------------------------------------------------------------------
# Credit guard (counter lives in the Meta tab, keyed by calendar month)
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


def monthly_cap():
    return int(os.environ.get("APOLLO_MONTHLY_CAP", DEFAULT_MONTHLY_CAP))


def _credit_key(month):
    return f"credits_{month}"


def credits_spent_this_month(month):
    """How many enrichment credits we've recorded this calendar month."""
    raw = _meta_get(_credit_key(month), "0")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _add_credits(month, n):
    new_total = credits_spent_this_month(month) + n
    _meta_set(_credit_key(month), new_total)
    return new_total


# ---------------------------------------------------------------------------
# Scoring — uses ONLY free search data
# ---------------------------------------------------------------------------
def _is_competitor(company):
    c = (company or "").lower()
    return any(kw in c for kw in COMPETITOR_NAME_KEYWORDS)


def _title_signal(title, pipeline):
    """Return 'strong', 'weak', or 'none' for how well a title fits the pipeline."""
    t = (title or "").lower()
    preset_titles = PUBLISHER_TITLES if pipeline == "publisher" else ADVERTISER_TITLES
    if any(phrase.lower() in t for phrase in preset_titles):
        return "strong"
    if any(kw in t for kw in TITLE_KEYWORDS.get(pipeline, [])):
        return "weak"
    return "none"


def score_candidate(person, preset):
    """
    Return (tier, reason). Tier is 'A', 'B', 'C', or 'DISQUALIFIED'.
      A = title clearly matches + in segment + has an email on file
      B = title matches but a weaker signal (strong title w/o email, or weak title w/ email)
      C = partial / weak match  (dropped, never enriched)
      DISQUALIFIED = competitor agency  (dropped, never stored)
    Segment match is implied: the search itself was filtered by the segment's
    organization keywords, so returned people are treated as in-segment.
    """
    org = person.get("organization", {}) or {}
    company = org.get("name") or person.get("organization_name") or ""
    if _is_competitor(company):
        return "DISQUALIFIED", f"competitor agency ({company})"

    has_email = bool(person.get("has_email"))
    signal = _title_signal(person.get("title", ""), preset["pipeline"])

    if signal == "strong" and has_email:
        return "A", "strong title + email on file"
    if signal == "strong" and not has_email:
        return "B", "strong title but no email on file"
    if signal == "weak" and has_email:
        return "B", "weaker title but email on file"
    if signal == "weak":
        return "C", "weak title, no email"
    return "C", "title does not match"


# ---------------------------------------------------------------------------
# Helpers to read fields off a person record
# ---------------------------------------------------------------------------
def _masked_name(person):
    first = person.get("first_name", "") or ""
    last = person.get("last_name") or person.get("last_name_obfuscated") or ""
    return (first + " " + last).strip() or "(unknown)"


def _location(person):
    parts = [person.get("city"), person.get("state"), person.get("country")]
    return ", ".join(p for p in parts if p) or "India"


def resolve_segment(text):
    """Map a loose phrase or key to a preset key, or None if unknown."""
    if not text:
        return None
    key = text.strip().lower()
    if key in SEARCH_PRESETS:
        return key
    if key in SEGMENT_ALIASES:
        return SEGMENT_ALIASES[key]
    # try the dashed form, e.g. "bfsi lending" -> "bfsi-lending"
    dashed = key.replace(" ", "-")
    if dashed in SEARCH_PRESETS:
        return dashed
    return None


# ---------------------------------------------------------------------------
# THE CORE FUNCTION
# ---------------------------------------------------------------------------
def source_leads(segment, max_enrich=10, dry_run=False, notify=None):
    """
    Source leads for one segment. Returns a summary dict. If `notify` is given
    (a function taking one string), a plain-English summary is sent to it.

    Steps: search (free) -> score (free) -> dedupe -> enrich top A/B keepers up
    to max_enrich AND the monthly credit cap -> write keepers as Status 'new'.
    dry_run=True does everything EXCEPT enrichment (0 credits).
    """
    def announce(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                log.exception("Could not post Slack summary.")

    key = resolve_segment(segment)
    if not key:
        msg = (f"⚠️ I don't have a search preset for '{segment}'. "
               f"Known segments: {', '.join(SEARCH_PRESETS)}.")
        announce(msg)
        return {"ok": False, "reason": "unknown_segment", "message": msg}

    preset = SEARCH_PRESETS[key]
    pipeline = preset["pipeline"]
    summary = {"ok": True, "segment": key, "pipeline": pipeline, "dry_run": dry_run}

    # (c) Honour Agent 1's pause switch.
    if is_segment_paused(key, pipeline):
        msg = f"⏸️ Skipped *{key}* ({pipeline}) — it's marked *paused* in the Segments tab. No credits used."
        announce(msg)
        summary.update({"skipped": True, "reason": "paused"})
        return summary

    # (a) FREE search — pull a few pages into a pool.
    pool, total = [], 0
    try:
        for page in range(1, SEARCH_POOL_PAGES + 1):
            people, total = apollo.search_people(
                titles=preset["person_titles"],
                locations=preset["person_locations"],
                organization_keywords=preset["organization_keywords"],
                page=page,
                per_page=SEARCH_PER_PAGE,
            )
            pool.extend(people)
            if len(people) < SEARCH_PER_PAGE:
                break  # no more pages
    except apollo.ApolloError as err:
        msg = f"❌ Apollo search failed for *{key}*: {err.message} (no credits used)."
        announce(msg)
        summary.update({"ok": False, "reason": "apollo_error", "message": err.message})
        return summary

    # Apollo can return the same person on more than one page. Dedupe by Apollo
    # id within this run so we never score — or enrich — the same person twice.
    seen_ids, unique_pool = set(), []
    for person in pool:
        pid = str(person.get("id", "")).strip()
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        unique_pool.append(person)
    pool = unique_pool

    # (b) Score for free; split into keepers (A/B), C, disqualified.
    already = existing_apollo_ids()
    tier_a, tier_b, n_c, n_disq, n_dupe = [], [], 0, 0, 0
    for person in pool:
        pid = str(person.get("id", "")).strip()
        tier, reason = score_candidate(person, preset)
        if tier == "DISQUALIFIED":
            n_disq += 1
            continue
        if tier == "C":
            n_c += 1
            continue
        if pid and pid in already:
            n_dupe += 1  # already saved earlier — never re-process or re-enrich
            continue
        (tier_a if tier == "A" else tier_b).append(person)

    # (e) Enrich Tier A first, then B, up to max_enrich AND the monthly cap.
    keepers = tier_a + tier_b
    month = sheets.now_utc()[:7]  # 'YYYY-MM' — counter auto-resets each month
    spent = credits_spent_this_month(month)
    cap = monthly_cap()
    remaining_budget = max(0, cap - spent)

    want = min(len(keepers), max_enrich)
    allowed = min(want, remaining_budget)
    to_enrich = keepers[:allowed]
    capped_by_budget = allowed < want

    summary.update({
        "total_in_apollo": total,
        "searched": len(pool),
        "tier_a": len(tier_a),
        "tier_b": len(tier_b),
        "qualified": len(keepers),
        "dropped_c": n_c,
        "disqualified": n_disq,
        "already_saved": n_dupe,
        "credits_before": spent,
        "monthly_cap": cap,
    })

    # (g) DRY RUN — show what it WOULD enrich, spend nothing.
    if dry_run:
        preview = "\n".join(
            f"   • [{('A' if p in tier_a else 'B')}] {_masked_name(p)} — "
            f"{p.get('title','?')} @ {(p.get('organization') or {}).get('name','?')}"
            for p in to_enrich
        ) or "   (nothing qualified to enrich)"
        msg = (
            f"🔍 *DRY RUN — {key} ({pipeline})* — 0 credits used\n"
            f"• Apollo has *{total:,}* people matching this search\n"
            f"• Pulled & scored *{len(pool)}* candidates\n"
            f"• Qualified keepers: *{len(keepers)}* (A: {len(tier_a)}, B: {len(tier_b)})\n"
            f"• Dropped — weak/Tier C: {n_c} · competitors: {n_disq} · already saved: {n_dupe}\n"
            f"• WOULD enrich *{len(to_enrich)}* (limit {max_enrich}, "
            f"{remaining_budget} credits left this month):\n{preview}"
        )
        announce(msg)
        summary.update({"would_enrich": len(to_enrich), "enriched": 0, "written": 0, "credits_used": 0})
        return summary

    # (e+f) REAL RUN — enrich, then write keepers. Stop cleanly on Apollo errors.
    ws = _leads_ws()
    enriched, written, credits_used = 0, 0, 0
    stopped_early = None
    for person in to_enrich:
        try:
            matched = apollo.enrich_person(person)
        except apollo.ApolloError as err:
            stopped_early = err.message  # e.g. rate-limit/credit error — stop, don't loop
            break
        credits_used += 1
        enriched += 1
        if not matched:
            continue

        # Email handling — we NEVER drop a lead for a missing/unverified email,
        # because a verified LinkedIn URL alone is valuable (outreach starts there).
        email = (matched.get("email") or "").strip()
        if "email_not_unlocked" in email or email.endswith("domain.com"):
            email = ""  # Apollo's locked placeholder — not a real address
        raw_status = (matched.get("email_status") or "").strip().lower()
        if email and raw_status == "verified":
            email_status, flag = "verified", ""
        elif email:  # present but guessed / likely / unverified — keep it, flag it
            email_status, flag = "unverified — review", "review"
        else:  # no usable email — still keep the lead for its LinkedIn URL
            email, email_status, flag = "", "none", ""

        row = [
            matched.get("name") or _masked_name(person),
            matched.get("title") or person.get("title", ""),
            (matched.get("organization") or {}).get("name")
                or (person.get("organization") or {}).get("name", ""),
            matched.get("linkedin_url", ""),
            email,
            email_status,
            flag,
            _location(matched) if matched.get("country") else _location(person),
            key, pipeline,
            "A" if person in tier_a else "B",
            "new",
            str(person.get("id", "")),
            sheets.now_utc(),
        ]
        if ws:
            id_col = LEADS_HEADERS.index("ApolloID")
            sheets.upsert_row(ws, key_columns=[id_col], new_row=row, key_values=[row[id_col]])
            written += 1

    # (4) Record the credits we actually spent (only successful enrichments).
    credits_after = _add_credits(month, credits_used) if credits_used else spent

    note = ""
    if capped_by_budget:
        note += f"\n⚠️ Monthly cap ({cap}) limited this run — only {allowed} of {want} enriched."
    if stopped_early:
        note += f"\n⚠️ Stopped early: {stopped_early}"
    if not ws:
        note += "\nℹ️ No Google Sheet connected, so nothing was written (local test mode)."

    msg = (
        f"✅ *Sourced {key} ({pipeline})*\n"
        f"• Searched & scored: *{len(pool)}* (Apollo total: {total:,})\n"
        f"• Qualified keepers: *{len(keepers)}* (A: {len(tier_a)}, B: {len(tier_b)})\n"
        f"• Dropped — weak: {n_c} · competitors: {n_disq} · already saved: {n_dupe}\n"
        f"• Enriched: *{enriched}*  → written as 'new': *{written}*\n"
        f"• Credits used: *{credits_used}*  (this month: {credits_after}/{cap}){note}"
    )
    announce(msg)
    summary.update({
        "enriched": enriched, "written": written, "credits_used": credits_used,
        "credits_after": credits_after, "stopped_early": stopped_early,
    })
    return summary


# ---------------------------------------------------------------------------
# Weekly scheduled run — pick the next active advertiser segment in rotation
# ---------------------------------------------------------------------------
def next_active_advertiser_segment():
    """Return the next non-paused advertiser segment after the last one we ran."""
    last = _meta_get("last_scheduled_segment", "")
    order = ADVERTISER_SEGMENTS
    start = (order.index(last) + 1) if last in order else 0
    rotation = order[start:] + order[:start]
    for seg in rotation:
        if not is_segment_paused(seg, "advertiser"):
            return seg
    return None  # everything is paused


def run_scheduled(notify=None, max_enrich=10):
    """Run one segment automatically, in rotation. Used by the weekly scheduler."""
    seg = next_active_advertiser_segment()
    if not seg:
        if notify:
            notify("⏸️ Weekly sourcing skipped — every advertiser segment is paused.")
        return {"ok": True, "skipped": True, "reason": "all_paused"}
    if notify:
        notify(f"🗓️ Weekly auto-sourcing starting for *{seg}* (advertiser)…")
    result = source_leads(seg, max_enrich=max_enrich, dry_run=False, notify=notify)
    _meta_set("last_scheduled_segment", seg)
    return result


# ---------------------------------------------------------------------------
# Run a DRY RUN from the terminal (0 credits), e.g.:
#     python3 sourcer.py bfsi-lending
#     python3 sourcer.py publisher 5
# It only ever previews — it never enriches or spends credits when run this way.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.disable(logging.CRITICAL)  # keep the output clean
    seg = sys.argv[1] if len(sys.argv) > 1 else "bfsi-lending"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    print(f"DRY RUN (0 credits) — segment '{seg}', max_enrich {limit}\n")
    source_leads(seg, max_enrich=limit, dry_run=True, notify=print)
