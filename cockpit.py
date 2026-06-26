"""
cockpit.py — Agent 4, "the Send Cockpit".

What it does, in plain English:
  1. Looks at the "Leads" tab for leads YOU have approved (DMApproval == "approve")
     that haven't been queued yet (SendStatus is empty).
  2. Up to a daily cap, marks each one SendStatus = "queued" and posts a tidy
     "card" to a Slack channel: the person's details, their LinkedIn link, and
     the exact first DM in a copy-friendly code block.
  3. Each card has buttons — [Mark Requested] [Mark Accepted] [Mark Messaged]
     [Skip] — so when YOU do the action on LinkedIn by hand, one click records
     where that lead is in the funnel (and the date) back onto the sheet.

It spends NO Apollo credits and NO Anthropic tokens — it only reads the sheet
and posts to Slack.

===========================================================================
CRITICAL SAFETY RULE — DO NOT VIOLATE:
  Agent 4 NEVER logs into LinkedIn, NEVER sends a message, connection request,
  or any other action, and NEVER automates anything on LinkedIn. It is a
  read-and-display assistant ONLY: it reads approved leads from the sheet and
  shows them to the HUMAN (Kawalpreet) in Slack to action manually. All sending
  is done by a human. The bot only records, on a human's button click or manual
  sheet edit, where a lead is in the funnel.

  It must ONLY ever queue leads where DMApproval == "approve" (case-insensitive).
  No other DMApproval value (pending / reject / edit / blank) may ever be
  queued or shown for sending.
===========================================================================

The Slack posting itself is done by app.py (which owns the Slack client), passed
in here as callbacks — exactly like the Sourcer's `notify`. The shared Google
Sheet connection lives in sheets.py.

Run a local DRY TEST from the terminal (0 posts, 0 sheet changes), e.g.:
    python3 cockpit.py          # show cards that WOULD be queued today
    python3 cockpit.py 3        # cap the preview at 3
"""

import logging
import os

import sheets
from sourcer import LEADS_HEADERS, LEADS_TAB

log = logging.getLogger("cockpit")

# Where the send cards are posted. Configurable via env so you can change it in
# Railway without touching code.
SEND_CHANNEL = os.environ.get("SEND_CHANNEL", "#outreach-send")

# How many leads to queue per day by default. Override with SEND_DAILY_CAP.
DEFAULT_DAILY_CAP = 5


def daily_cap() -> int:
    try:
        return int(os.environ.get("SEND_DAILY_CAP", DEFAULT_DAILY_CAP))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


# The two columns Agent 4 ADDS to the Leads tab, immediately after DMApproval.
#   - SendStatus: the send lifecycle. NEVER auto-advanced — only a human button
#     click or a manual sheet edit changes it:
#       "" (not queued) → "queued" → "requested" → "accepted" → "messaged"
#         → "replied"
#       ("skipped" is a side exit when you choose Skip on a card.)
#   - SendDate: the date you last acted on this lead (set on each button click).
SEND_HEADERS = ["SendStatus", "SendDate"]

# The only DMApproval value we ever act on (see the SAFETY RULE above).
APPROVED_VALUE = "approve"

# Button action ids (registered in app.py) → the SendStatus they record.
BUTTON_STATUS = {
    "send_mark_requested": "requested",
    "send_mark_accepted": "accepted",
    "send_mark_messaged": "messaged",
    "send_skip": "skipped",
}


def _col_letter(index_zero_based: int) -> str:
    """Turn a 0-based column number into a spreadsheet letter (0→A, 26→AA)."""
    letter, n = "", index_zero_based + 1
    while n:
        n, remainder = divmod(n - 1, 26)
        letter = chr(ord("A") + remainder) + letter
    return letter


def _leads_ws():
    ss = sheets.open_spreadsheet()
    return sheets.ensure_tab(ss, LEADS_TAB, LEADS_HEADERS) if ss else None


def _ensure_send_columns(leads_ws) -> list:
    """
    Make sure SendStatus + SendDate exist on the Leads tab (added after the
    Personalizer's columns, i.e. right after DMApproval). Returns the current
    header row. Safe to call repeatedly — only adds what's missing, and grows
    the grid first if it's too narrow.
    """
    header = leads_ws.row_values(1)
    missing = [h for h in SEND_HEADERS if h not in header]
    if missing:
        start = len(header)  # 0-based index of the first new column
        end = start + len(missing) - 1
        if leads_ws.col_count < end + 1:
            leads_ws.add_cols(end + 1 - leads_ws.col_count)
        leads_ws.update(
            range_name=f"{_col_letter(start)}1:{_col_letter(end)}1",
            values=[missing],
        )
        header = header + missing
        log.info("Added send columns to Leads tab: %s", ", ".join(missing))
    return header


def _today() -> str:
    """Today's date (YYYY-MM-DD), reusing the shared timestamp helper."""
    return sheets.now_utc()[:10]


def _lead_identifier(lead: dict) -> str:
    """
    A stable handle for a lead, embedded in each button so a click can find the
    right row again. Prefers the Apollo id (unique), then the LinkedIn URL, then
    the sheet row number as a last resort.
    """
    if lead.get("ApolloID"):
        return "apollo:" + lead["ApolloID"]
    if lead.get("LinkedIn URL"):
        return "url:" + lead["LinkedIn URL"]
    return "row:" + str(lead.get("_row", ""))


def _build_card(lead: dict) -> dict:
    """
    Build one Slack 'card' for a lead: Block Kit blocks + a plain-text fallback +
    a readable text preview (used by the local dry test). No Slack call here —
    app.py does the posting.
    """
    name = lead.get("Name", "") or "(no name)"
    title = lead.get("Title", "") or "—"
    company = lead.get("Company", "") or "—"
    tier = lead.get("Tier", "") or "—"
    linkedin = lead.get("LinkedIn URL", "") or ""
    first_dm = lead.get("First DM", "") or "(no first DM on file)"
    ident = _lead_identifier(lead)

    link_line = (
        f"<{linkedin}|Open LinkedIn profile>" if linkedin else "_no LinkedIn URL on file_"
    )
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{name}* — {title} @ {company}\nTier: *{tier}*",
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": link_line}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*First DM (copy & send):*\n```{first_dm}```"},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Mark Requested"},
                 "action_id": "send_mark_requested", "value": ident},
                {"type": "button", "text": {"type": "plain_text", "text": "Mark Accepted"},
                 "action_id": "send_mark_accepted", "value": ident},
                {"type": "button", "text": {"type": "plain_text", "text": "Mark Messaged"},
                 "action_id": "send_mark_messaged", "value": ident},
                {"type": "button", "text": {"type": "plain_text", "text": "Skip"},
                 "style": "danger", "action_id": "send_skip", "value": ident},
            ],
        },
        {"type": "divider"},
    ]

    text_preview = (
        f"{name} — {title} @ {company} (Tier {tier})\n"
        f"LinkedIn: {linkedin or '(none)'}\n"
        f"First DM:\n{first_dm}"
    )
    return {
        "name": name,
        "company": company,
        "identifier": ident,
        "blocks": blocks,
        "text": f"Outreach card: {name} at {company}",  # Slack notification fallback
        "preview": text_preview,
    }


def queue_sends(limit=None, dry_run: bool = False, post_card=None, notify=None) -> dict:
    """
    Find approved, not-yet-queued leads and queue them for manual sending.

    A lead is eligible ONLY when DMApproval == "approve" (case-insensitive) AND
    SendStatus is empty. For each eligible lead (up to `limit`, default the daily
    cap), we build a card; on a REAL run we mark SendStatus="queued" and post the
    card via `post_card`. A lead already queued/requested/etc. is never re-queued.

    dry_run=True builds the cards but posts NOTHING and changes NOTHING on the
    sheet — used for a safe local preview.

    `post_card` is a function taking one card dict (it posts the Slack message).
    `notify` is a function taking one string (a short summary line).

    Returns: {ok, dry_run, count, cards:[...], message?}.
    """
    def announce(msg):
        log.info(msg)
        if notify:
            try:
                notify(msg)
            except Exception:
                log.exception("Could not post Slack summary.")

    if limit is None:
        limit = daily_cap()
    limit = int(limit)

    leads_ws = _leads_ws()
    if not leads_ws:
        msg = "ℹ️ No Google Sheet connected, so there are no leads to queue."
        announce(msg)
        return {"ok": False, "reason": "no_sheet", "message": msg, "cards": [], "count": 0}

    # On a dry run we must not touch the sheet, so read whatever header exists;
    # otherwise make sure our columns are present first.
    header = leads_ws.row_values(1) if dry_run else _ensure_send_columns(leads_ws)
    rows = leads_ws.get_all_values()

    def col(name):
        return header.index(name) if name in header else -1

    approval_col = col("DMApproval")
    status_col = col("SendStatus")

    cards = []
    for i in range(1, len(rows)):  # skip the header row
        if len(cards) >= limit:
            break
        row = rows[i]

        approval = (row[approval_col].strip().lower()
                    if 0 <= approval_col < len(row) else "")
        # SAFETY: only ever act on explicitly approved leads.
        if approval != APPROVED_VALUE:
            continue

        send_status = (row[status_col].strip().lower()
                       if 0 <= status_col < len(row) else "")
        if send_status:
            continue  # already queued / requested / etc. — never re-queue

        lead = {
            field: (row[col(field)] if 0 <= col(field) < len(row) else "")
            for field in ("Name", "Title", "Company", "Tier", "LinkedIn URL",
                          "First DM", "ApolloID")
        }
        lead["_row"] = i + 1  # spreadsheet row number (1-based)
        card = _build_card(lead)

        if not dry_run:
            # Mark queued first so a posting error can't leave it un-tracked twice.
            start = col("SendStatus")
            end = col("SendDate")
            leads_ws.update(
                range_name=f"{_col_letter(start)}{lead['_row']}:{_col_letter(end)}{lead['_row']}",
                values=[["queued", _today()]],
            )
            if post_card:
                post_card(card)

        cards.append(card)

    count = len(cards)
    if dry_run:
        announce(f"🔍 DRY RUN — {count} approved lead(s) would be queued to {SEND_CHANNEL}.")
    elif count:
        announce(f"📬 Queued *{count}* lead(s) to {SEND_CHANNEL} for manual sending.")
    else:
        announce(
            "No approved leads are waiting to be queued. (A lead must have "
            "DMApproval = 'approve' and no SendStatus yet.)"
        )

    return {"ok": True, "dry_run": dry_run, "count": count, "cards": cards}


def mark_status(identifier: str, status: str) -> dict:
    """
    Record a lead's send status from a button click (or programmatic call). Finds
    the lead by the identifier embedded in the button, sets SendStatus=status and
    SendDate=today. Returns {ok, name, company, status} or {ok:False, message}.

    This is the ONLY thing that changes a lead's SendStatus other than a manual
    sheet edit — status is never auto-advanced.
    """
    leads_ws = _leads_ws()
    if not leads_ws:
        return {"ok": False, "message": "No Google Sheet connected."}

    header = _ensure_send_columns(leads_ws)
    rows = leads_ws.get_all_values()

    def col(name):
        return header.index(name) if name in header else -1

    kind, _, value = identifier.partition(":")
    key_col = {"apollo": col("ApolloID"), "url": col("LinkedIn URL")}.get(kind, -1)

    target_row = None
    if kind == "row":
        try:
            target_row = int(value)
        except ValueError:
            target_row = None
    elif key_col >= 0:
        for i in range(1, len(rows)):
            cell = rows[i][key_col] if key_col < len(rows[i]) else ""
            if cell.strip() == value.strip():
                target_row = i + 1
                break

    if not target_row or target_row < 2 or target_row > len(rows):
        return {"ok": False, "message": f"Couldn't find that lead ({identifier})."}

    row = rows[target_row - 1]
    name = row[col("Name")] if 0 <= col("Name") < len(row) else "(lead)"
    company = row[col("Company")] if 0 <= col("Company") < len(row) else ""

    start = col("SendStatus")
    end = col("SendDate")
    today = _today()
    leads_ws.update(
        range_name=f"{_col_letter(start)}{target_row}:{_col_letter(end)}{target_row}",
        values=[[status, today]],
    )
    return {"ok": True, "name": name, "company": company, "status": status, "date": today}


# ---------------------------------------------------------------------------
# Local DRY TEST (0 Slack posts, 0 sheet changes), e.g.:
#     python3 cockpit.py        # all approved leads (up to the daily cap)
#     python3 cockpit.py 3      # cap the preview at 3
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    load_dotenv()
    logging.disable(logging.CRITICAL)  # keep the output clean
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else daily_cap()
    result = queue_sends(limit=limit, dry_run=True)
    cards = result.get("cards", [])
    if not result.get("ok"):
        print(result.get("message", "Could not run."))
    elif not cards:
        print("No approved leads are waiting to be queued.")
        print("(Expected if you haven't set any lead's DMApproval to 'approve' yet.)")
    else:
        print(f"DRY TEST — {len(cards)} card(s) WOULD be posted to {SEND_CHANNEL} "
              f"(nothing posted, nothing changed):\n")
        for n, card in enumerate(cards, 1):
            print(f"########## CARD {n} ##########")
            print(card["preview"])
            print("[ Mark Requested ] [ Mark Accepted ] [ Mark Messaged ] [ Skip ]\n")
