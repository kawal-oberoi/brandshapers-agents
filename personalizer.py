"""
personalizer.py — Agent 3, "the Personalizer".

What it does, in plain English:
  1. Looks at the "Leads" tab (filled by Agent 2, the Sourcer) for leads that
     don't have outreach messages yet (MsgStatus empty or "new").
  2. For each one, asks Claude (Opus) to write ready-to-send LinkedIn outreach
     tailored to that person — a first DM plus two follow-ups.
  3. Writes those messages onto the SAME row and marks MsgStatus = "drafted".
  4. Sets DMApproval = "pending" — a HUMAN approval gate on the first DM only.

It spends NO Apollo credits — only Anthropic tokens. It never touches a lead's
own details (those belong to the Sourcer) and never re-writes a row that is
already "drafted".

The shared Google Sheet connection lives in sheets.py and is used by every
agent. The Leads-tab schema is owned by the Sourcer, so we import it from there
and only ADD our message columns after it.

Run a local dry run from the terminal (0 writes, only tokens), e.g.:
    python3 personalizer.py          # preview messages for all new leads
    python3 personalizer.py 3        # preview at most 3
"""

import json
import logging
import os
import re

import anthropic

import sheets
from sourcer import LEADS_HEADERS, LEADS_TAB

log = logging.getLogger("personalizer")

# The model we use. claude-opus-4-8 is the latest, most capable Claude model;
# the fallback is used only if that exact string is ever rejected.
PREFERRED_MODEL = "claude-opus-4-8"
FALLBACK_MODEL = "claude-opus-4-8"
active_model = PREFERRED_MODEL

# Where summaries are posted (kept in step with the Sourcer's channel).
OUTREACH_CHANNEL = "#outreach-control"

_claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

# ---------------------------------------------------------------------------
# The columns Agent 3 ADDS to the Leads tab, immediately after the Sourcer's
# columns (i.e. right after DateAdded).
#   - Connection Note: always the literal "(blank request)" — we deliberately
#     send blank LinkedIn connection requests (higher acceptance, no monthly
#     cap), so this marker means "intentionally blank", not "missing".
#   - First DM / Followup1 / Followup2: the actual messages, written by Opus.
#   - MsgStatus: ""/"new" = not generated yet; "drafted" = ready to send.
#   - DMApproval: a HUMAN approval gate for the First DM only. Agent 3 sets it
#     to "pending" when it drafts. Kawalpreet then sets it by hand to one of:
#       * "approve" — the First DM (whatever text is in the cell at send time,
#                     so an edit = overwrite the First DM cell + set "approve")
#                     is cleared to go out.
#       * "reject"  — this lead must NEVER be contacted.
#       * "edit"    — (optional, informational) being worked on; not yet approved.
#     The follow-ups are NOT gated — they go out automatically once sending runs.
#
# ===========================================================================
# RULE FOR AGENT 4 (the sender, built next) — DO NOT VIOLATE:
#   Whatever sends outreach must ONLY ever act on a lead whose DMApproval cell
#   equals "approve" (case-insensitive). Any other value — "pending", "reject",
#   "edit", or blank — means DO NOT SEND the First DM to that lead. The First DM
#   text to send is exactly what is in the "First DM" cell at send time (this is
#   how human edits take effect). Follow-ups are not gated by DMApproval.
# ===========================================================================
MESSAGE_HEADERS = [
    "Connection Note", "First DM", "Followup1", "Followup2", "MsgStatus",
    "DMApproval",
]

# We deliberately send BLANK LinkedIn connection requests. This marker records
# that the blank is intentional, not a message we forgot to write.
BLANK_CONNECTION_NOTE = "(blank request)"

# ---------------------------------------------------------------------------
# How Opus should write the messages. The big rules live here; per-lead details
# (name, role, company, segment) and a per-lead variation guide are added at
# call time so a batch doesn't read like one template with the names swapped.
# ---------------------------------------------------------------------------
PERSONALIZER_SYSTEM_PROMPT = (
    "You write LinkedIn outreach for Brand Shapers, a performance and affiliate "
    "marketing agency. You write on behalf of Kawalpreet. You produce THREE "
    "short messages for ONE lead: a first DM, and two follow-ups.\n\n"
    "ABOUT BRAND SHAPERS' MODEL (for ADVERTISER leads — companies that want "
    "users/customers):\n"
    "  - Pay-for-performance user acquisition: the advertiser pays ONLY on real "
    "results — NOT on clicks or impressions.\n"
    "  - What counts as a 'result' depends on the advertiser's product, so use "
    "whichever fits naturally — a verified signup, a first loan, a first "
    "transaction, a funded account, etc. Do NOT assume everyone is a "
    "mobile-app-install funnel; many lenders are web/branch-led. Pick the "
    "outcome that suits THIS company rather than defaulting to 'install'.\n"
    "  - Every result is fraud-screened.\n"
    "  - Delivered through an affiliate / publisher network.\n\n"
    "ABOUT BRAND SHAPERS' MODEL (for PUBLISHER leads — partners who have "
    "traffic/audience to monetise): pitch the OTHER side instead — exclusive "
    "offers, strong payouts, reliable on-time wire payments, and that we can "
    "send them steady volume.\n\n"
    "HOW TO WRITE THE FIRST DM (this is the real pitch, sent after they accept "
    "the connection):\n"
    "  1. OPEN ON THEIR WORLD. Show you understand what someone in THEIR role "
    "and segment actually cares about. Examples: a lending app cares about "
    "cost-per-acquisition and user retention; a gaming studio cares about "
    "install quality and fraud. Make the opening specific to this person.\n"
    "  2. Then position the Brand Shapers model above in ONE or TWO sentences.\n"
    "  3. END with a low-pressure call to action framed as an OFFER, not a "
    "request — offer to SHARE something relevant (an example, a quick "
    "breakdown, a relevant case). NEVER say 'let's hop on a call' or ask for a "
    "meeting.\n"
    "  4. Sign off as 'Kawalpreet'.\n"
    "  Keep it CONCISE — a LinkedIn DM, not an email. A few short sentences.\n\n"
    "FOLLOW-UP 1 (sent 3-4 days later if no reply): short and light; offer "
    "something useful. Do NOT sign off again.\n"
    "FOLLOW-UP 2 (sent 7 days later, the final nudge): brief, low-pressure, "
    "leaves the door open. Do NOT sign off again.\n\n"
    "VARIETY (IMPORTANT): these messages go to different people, so they must "
    "NOT read like the same template with names swapped. Each request includes "
    "a 'Variation guide' — follow it for the opening angle and the CTA, and "
    "vary your sentence structure and rhythm too (e.g. don't always open with "
    "'in lending the hard part is…'). Make this one feel individually written.\n\n"
    "HARD RULES:\n"
    "  - NEVER invent results, client names, statistics, or numbers. No fake "
    "metrics, no 'we helped X grow by Y%'. Speak about the model, not made-up "
    "outcomes.\n"
    "  - Value-first and professional throughout. No hype, no spammy lines.\n"
    "  - Write naturally, as a real person would.\n\n"
    "OUTPUT FORMAT: respond with ONLY a JSON object, no other text, exactly:\n"
    '{"first_dm": "...", "followup1": "...", "followup2": "..."}'
)

# Rotating hints so a batch of leads doesn't come out as one template with the
# names swapped. We pick one opening angle + one CTA style per lead (by its
# position in the batch) and hand them to Opus as a "Variation guide".
_ADVERTISER_ANGLES = [
    "open on cost-per-acquisition pressure — paying for traffic that doesn't convert",
    "open on lead/customer QUALITY and fraud — junk leads that never become real users",
    "open on RETENTION — acquiring users who actually stick rather than one-and-done",
    "open on wasted ad spend on clicks/impressions with no accountability for outcomes",
]
_PUBLISHER_ANGLES = [
    "open on getting access to exclusive, high-converting offers",
    "open on payout strength — earning more per conversion",
    "open on reliability — getting paid on time, every time, by wire",
    "open on steady volume — a partner who can keep their inventory monetised",
]
_CTA_STYLES = [
    "offer to share a short, concrete breakdown of how it could map to their funnel",
    "offer to send a relevant example from a similar business in their space",
    "offer to put together a quick teardown of where they might be leaking spend/value",
    "offer a useful reference they can take to their team, no strings attached",
]


def _col_letter(index_zero_based: int) -> str:
    """Turn a 0-based column number into a spreadsheet letter (0→A, 26→AA)."""
    letter, n = "", index_zero_based + 1
    while n:
        n, remainder = divmod(n - 1, 26)
        letter = chr(ord("A") + remainder) + letter
    return letter


def _extract_json_object(text: str) -> dict:
    """Pull the JSON object out of Claude's reply, tolerating any stray text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _generate_messages_for_lead(lead: dict, variation: int = 0) -> dict:
    """
    Ask Opus to write the three messages for one lead. `lead` has the keys
    Name, Title, Company, Segment, Pipeline. `variation` (the lead's position in
    the batch) rotates the opening angle + CTA so a batch reads as distinct
    messages, not one template. Returns a dict with keys connection_note,
    first_dm, followup1, followup2.
    """
    global active_model

    pipeline = (lead.get("Pipeline") or "").strip().lower()
    is_publisher = pipeline == "publisher"
    audience = "PUBLISHER" if is_publisher else "ADVERTISER"
    angles = _PUBLISHER_ANGLES if is_publisher else _ADVERTISER_ANGLES
    angle = angles[variation % len(angles)]
    cta = _CTA_STYLES[variation % len(_CTA_STYLES)]
    user_prompt = (
        f"Write the outreach for this {audience} lead.\n"
        f"Name: {lead.get('Name', '')}\n"
        f"Title: {lead.get('Title', '')}\n"
        f"Company: {lead.get('Company', '')}\n"
        f"Segment: {lead.get('Segment', '')}\n"
        f"Pipeline: {lead.get('Pipeline', '')}\n\n"
        f"Variation guide for THIS lead:\n"
        f"  - Opening angle: {angle}.\n"
        f"  - CTA: {cta}.\n"
        f"  - Use a different sentence structure/opening from a generic template."
    )

    for _ in range(2):  # one retry only to swap the model if it's rejected
        try:
            response = _claude.messages.create(
                model=active_model,
                max_tokens=1024,
                system=PERSONALIZER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
            break
        except (anthropic.NotFoundError, anthropic.BadRequestError) as error:
            if active_model != FALLBACK_MODEL:
                log.warning(
                    "Model %r rejected (%s); falling back to %r.",
                    active_model, error.__class__.__name__, FALLBACK_MODEL,
                )
                active_model = FALLBACK_MODEL
                continue
            raise

    text = "".join(b.text for b in response.content if b.type == "text")
    parsed = _extract_json_object(text)
    return {
        "connection_note": BLANK_CONNECTION_NOTE,
        "first_dm": parsed.get("first_dm", "").strip(),
        "followup1": parsed.get("followup1", "").strip(),
        "followup2": parsed.get("followup2", "").strip(),
    }


def _ensure_message_columns(leads_ws) -> list:
    """
    Make sure the message columns exist on the Leads tab (added after the
    Sourcer's columns, i.e. right after DateAdded). Returns the current header
    row. Safe to call repeatedly — it only adds columns that are missing, and
    grows the grid first if it's too narrow.
    """
    header = leads_ws.row_values(1)
    missing = [h for h in MESSAGE_HEADERS if h not in header]
    if missing:
        start = len(header)  # 0-based index of the first new column
        end = start + len(missing) - 1
        needed_cols = end + 1
        if leads_ws.col_count < needed_cols:
            leads_ws.add_cols(needed_cols - leads_ws.col_count)
        leads_ws.update(
            range_name=f"{_col_letter(start)}1:{_col_letter(end)}1",
            values=[missing],
        )
        header = header + missing
        log.info("Added message columns to Leads tab: %s", ", ".join(missing))
    return header


def _leads_ws():
    ss = sheets.open_spreadsheet()
    return sheets.ensure_tab(ss, LEADS_TAB, LEADS_HEADERS) if ss else None


def _sheet_url():
    ss = sheets.open_spreadsheet()
    try:
        return ss.url if ss else ""
    except Exception:
        return ""


def _build_summary(drafted: list, dry_run: bool) -> str:
    """A plain-English Slack summary: how many DMs are pending + a short preview."""
    count = len(drafted)
    if dry_run:
        lines = [f"🔍 *DRY RUN* — would draft {count} first DM(s). Nothing written."]
    else:
        lines = [
            f"✍️ Drafted *{count}* first DM(s) — all set to *pending* approval "
            f"in the Leads tab."
        ]
    for item in drafted[:2]:
        lines.append("")
        lines.append(f"*{item['name']}* — First DM:")
        lines.append(item["messages"]["first_dm"])
    if count > 2:
        lines.append("")
        lines.append(f"…and {count - 2} more on the Leads tab.")
    if not dry_run:
        lines.append("")
        lines.append(
            "Please approve/reject/edit each in the *DMApproval* column: "
            "`approve` to send, `reject` to skip, or overwrite the First DM cell "
            "with your own text and set `approve`. Follow-ups go out automatically."
        )
        url = _sheet_url()
        if url:
            lines.append(f"Sheet: {url}")
    return "\n".join(lines)


def generate_messages(limit: int = 10, dry_run: bool = False, notify=None) -> dict:
    """
    Find leads that don't have messages yet and write outreach onto their row.

    A lead "needs messages" when its MsgStatus is empty or "new". For each such
    lead (up to `limit`), Opus writes the three messages; we then fill in
    Connection Note / First DM / Followup1 / Followup2, set MsgStatus="drafted"
    and DMApproval="pending". Rows that are already "drafted" (or anything else)
    are left untouched — we never overwrite work that's done, and the follow-ups
    are never gated by approval.

    If `dry_run` is True we generate and RETURN the messages but write NOTHING to
    the sheet — used to preview quality. If `notify` is given (a function taking
    one string) a plain-English summary is sent to it.

    Returns a summary dict: {ok, dry_run, count, drafted:[...], message?}.
    """
    def announce(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                log.exception("Could not post Slack summary.")

    leads_ws = _leads_ws()
    if not leads_ws:
        msg = "ℹ️ No Google Sheet connected, so there are no leads to personalize."
        announce(msg)
        return {"ok": False, "reason": "no_sheet", "message": msg, "drafted": [], "count": 0}

    # In a dry run we must not touch the sheet, so we read whatever header is
    # there; otherwise we make sure our columns exist first.
    header = leads_ws.row_values(1) if dry_run else _ensure_message_columns(leads_ws)
    rows = leads_ws.get_all_values()

    def col(name):
        return header.index(name) if name in header else -1

    status_col = col("MsgStatus")
    drafted = []

    for i in range(1, len(rows)):  # skip the header row
        if len(drafted) >= limit:
            break
        row = rows[i]
        status = (
            row[status_col].strip().lower()
            if 0 <= status_col < len(row) else ""
        )
        if status not in ("", "new"):
            continue  # already drafted or beyond — never overwrite

        lead = {
            field: (row[col(field)] if 0 <= col(field) < len(row) else "")
            for field in ("Name", "Title", "Company", "Segment", "Pipeline")
        }
        if not lead["Name"].strip():
            continue  # blank/spacer row

        log.info("Generating messages for %s (%s)…", lead["Name"], lead["Company"])
        messages = _generate_messages_for_lead(lead, variation=len(drafted))

        if not dry_run:
            new_values = [
                messages["connection_note"],
                messages["first_dm"],
                messages["followup1"],
                messages["followup2"],
                "drafted",
                "pending",  # DMApproval — awaits human approve/reject/edit
            ]
            start = col("Connection Note")
            end = col("DMApproval")
            sheet_row = i + 1  # spreadsheet rows are 1-based
            leads_ws.update(
                range_name=(
                    f"{_col_letter(start)}{sheet_row}:"
                    f"{_col_letter(end)}{sheet_row}"
                ),
                values=[new_values],
            )

        drafted.append({
            "name": lead["Name"],
            "company": lead["Company"],
            "pipeline": lead["Pipeline"],
            "messages": messages,
        })

    if drafted:
        announce(_build_summary(drafted, dry_run))
    else:
        announce("No new leads to draft — every lead already has messages.")

    return {"ok": True, "dry_run": dry_run, "count": len(drafted), "drafted": drafted}


# ---------------------------------------------------------------------------
# Run a DRY RUN from the terminal (0 writes, only tokens), e.g.:
#     python3 personalizer.py        # all new leads
#     python3 personalizer.py 3      # at most 3
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.disable(logging.CRITICAL)  # keep the output clean
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    result = generate_messages(limit=limit, dry_run=True)
    drafted = result.get("drafted", [])
    print(f"DRY RUN (0 writes) — generated messages for {len(drafted)} lead(s).\n")
    for n, item in enumerate(drafted, 1):
        m = item["messages"]
        print(f"########## LEAD {n}: {item['name']} — {item['company']} "
              f"(pipeline: {item['pipeline']}) ##########")
        print(f"\n--- Connection Note ---\n{m['connection_note']}")
        print(f"\n--- First DM ---\n{m['first_dm']}")
        print(f"\n--- Followup1 (3-4 days) ---\n{m['followup1']}")
        print(f"\n--- Followup2 (7 days) ---\n{m['followup2']}\n\n")
