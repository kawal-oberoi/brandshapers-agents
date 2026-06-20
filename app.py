"""
Agent 1 — a Slack bot for Brand Shapers.

What it does:
  * Connects to Slack using Socket Mode (no public URL or webhooks needed).
  * Listens for messages in any channel the bot has been added to, and when a
    HUMAN posts (a plain message or an @mention) it talks to Claude.
  * Uses a Google Sheet as its single source of truth. Claude has real tools it
    can call to read and write that sheet:
        - update_segment        → add/update a row in the "Segments" tab
        - add_or_update_brief    → add/update a row in the "Briefs" tab
        - read_state             → read both tabs to answer "what's active?"
  * Posts Claude's reply back as a threaded reply in the same channel.
  * Ignores messages from bots and from itself, so it never replies to itself.

You do not need to edit this file to use the bot. Secrets and the Sheet ID are
read from environment variables (loaded from a local .env file in development).
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic
import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# Load secrets from a local .env file when running on your own machine.
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("agent-1")

# The model we want to use, plus the fallback if that exact string ever stops
# being available. claude-opus-4-8 is the latest, most capable Claude model.
PREFERRED_MODEL = "claude-opus-4-8"
FALLBACK_MODEL = "claude-opus-4-8"
active_model = PREFERRED_MODEL

SYSTEM_PROMPT = (
    "You are Agent 1, the operations brain for Brand Shapers, a performance "
    "and affiliate marketing agency. The user (Kawalpreet) talks to you in "
    "plain English about LinkedIn outreach operations — e.g. adding or pausing "
    "a target vertical or segment, adding a new campaign brief, marking a "
    "campaign as discontinued, or changing the ideal customer profile. Keep "
    "replies short and businesslike.\n\n"
    "CRITICAL — HOW YOU KNOW THINGS:\n"
    "You have NO memory of your own. You do not remember anything between "
    "messages — every message starts from a blank slate. A Google Sheet is "
    "your ONE AND ONLY source of truth, and the tools below are the ONLY way "
    "to read or change it.\n\n"
    "  - read_state: returns the current contents of both the Segments and "
    "Briefs tabs.\n"
    "  - update_segment: add or update a target vertical/segment in the "
    "Segments tab (e.g. pause or activate 'mobile gaming').\n"
    "  - add_or_update_brief: add or update a campaign brief in the Briefs tab "
    "(e.g. add a new brief or mark one discontinued).\n\n"
    "RULES YOU MUST FOLLOW:\n"
    "1. For ANY question about the current state — what is active, paused, "
    "discontinued, or recorded, or 'do we have X' — you MUST call read_state "
    "FIRST, then answer strictly from what it returns.\n"
    "2. You must NEVER say you have no memory, no record, or no information "
    "about prior state without calling read_state first. If, after calling "
    "read_state, there are no matching rows, then say there are none — but only "
    "after calling it.\n"
    "3. To record any change, you MUST call the correct write tool "
    "(update_segment for segments, add_or_update_brief for briefs), then "
    "confirm exactly what you recorded — e.g. 'Recorded: mobile gaming paused "
    "— both pipelines.'\n\n"
    "Pipelines are 'advertiser', 'publisher', or 'both'. When you restate "
    "intent, give a short structured summary (what changed, which pipeline, key "
    "details). Ask one concise clarifying question only if something essential "
    "is missing — but never let a missing detail stop you from calling "
    "read_state for a state question."
)

# Read the Slack + Anthropic secrets and the Sheet ID. We fail fast with a clear
# message if any are missing, so you immediately know what to fix.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

_missing = [
    name
    for name, value in (
        ("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN),
        ("SLACK_APP_TOKEN", SLACK_APP_TOKEN),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
        ("GOOGLE_SHEET_ID", GOOGLE_SHEET_ID),
    )
    if not value
]
if _missing:
    raise SystemExit(
        "Missing required environment variable(s): "
        + ", ".join(_missing)
        + ".\nCreate a .env file (copy .env.example) and fill in your values."
    )

app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Slack identifies the bot by a user ID like "U0123ABCD". We ask Slack for it
# once at startup so we can (a) strip the bot's own @mention out of message
# text and (b) tell when a plain message is actually an @mention.
BOT_USER_ID = app.client.auth_test()["user_id"]


def strip_bot_mention(text: str) -> str:
    """Remove the bot's @mention (e.g. "<@U0123ABCD>") from a message."""
    return re.sub(rf"<@{BOT_USER_ID}(\|[^>]+)?>", "", text).strip()


def mentions_bot(text: str) -> bool:
    """True if the text contains an @mention of this bot."""
    return bool(re.search(rf"<@{BOT_USER_ID}(\|[^>]+)?>", text))


# ---------------------------------------------------------------------------
# Google Sheet — the source of truth
# ---------------------------------------------------------------------------

# We only need access to spreadsheets. The Sheet must be shared (as Editor)
# with the service account's email address.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Tab names and their header rows.
SEGMENTS_TAB = "Segments"
SEGMENTS_HEADERS = ["Segment", "Pipeline", "Status", "Updated", "Notes"]
BRIEFS_TAB = "Briefs"
BRIEFS_HEADERS = ["Company", "Vertical", "Pipeline", "Status", "Details", "Updated"]


def _load_google_credentials() -> Credentials:
    """
    Build Google credentials from either:
      * the GOOGLE_CREDENTIALS_JSON environment variable (used on Railway), or
      * the local google-credentials.json file (used on your own machine).
    The environment variable is preferred when it is set.
    """
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        info = json.loads(raw)
        return Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
    if os.path.exists("google-credentials.json"):
        return Credentials.from_service_account_file(
            "google-credentials.json", scopes=GOOGLE_SCOPES
        )
    raise SystemExit(
        "No Google credentials found. Either set GOOGLE_CREDENTIALS_JSON or put "
        "google-credentials.json in the project folder."
    )


def _ensure_tab(spreadsheet, title: str, headers: list) -> "gspread.Worksheet":
    """Return the named tab, creating it with a header row if it doesn't exist."""
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=200, cols=len(headers))
        worksheet.append_row(headers)
        log.info("Created missing tab %r with headers.", title)
        return worksheet
    # Tab exists but is empty — add the header row.
    if not worksheet.row_values(1):
        worksheet.append_row(headers)
        log.info("Added header row to existing empty tab %r.", title)
    return worksheet


log.info("Connecting to Google Sheet %s…", GOOGLE_SHEET_ID)
_gc = gspread.authorize(_load_google_credentials())
_spreadsheet = _gc.open_by_key(GOOGLE_SHEET_ID)
segments_ws = _ensure_tab(_spreadsheet, SEGMENTS_TAB, SEGMENTS_HEADERS)
briefs_ws = _ensure_tab(_spreadsheet, BRIEFS_TAB, BRIEFS_HEADERS)
log.info("Google Sheet ready (tabs: %s, %s).", SEGMENTS_TAB, BRIEFS_TAB)


def _now() -> str:
    """A readable UTC timestamp for the 'Updated' column."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _upsert_row(worksheet, key_columns: list, new_row: list, key_values: list) -> bool:
    """
    Add or update a row. If a row already exists whose values in `key_columns`
    (0-based indexes) match `key_values` (case-insensitively), overwrite it;
    otherwise append a new row. Returns True if an existing row was updated.
    """
    existing_rows = worksheet.get_all_values()  # includes the header row
    wanted = [str(v).strip().lower() for v in key_values]
    # Start at index 1 to skip the header row.
    for i in range(1, len(existing_rows)):
        row = existing_rows[i]
        actual = [
            (row[c].strip().lower() if c < len(row) else "") for c in key_columns
        ]
        if actual == wanted:
            sheet_row = i + 1  # spreadsheet rows are 1-based
            last_col = chr(ord("A") + len(new_row) - 1)
            worksheet.update(
                range_name=f"A{sheet_row}:{last_col}{sheet_row}", values=[new_row]
            )
            return True
    worksheet.append_row(new_row)
    return False


# ---------------------------------------------------------------------------
# Tools Claude can call
# ---------------------------------------------------------------------------


def update_segment(segment: str, pipeline: str, status: str, notes: str = "") -> str:
    """Add or update a segment row. Keyed by (Segment, Pipeline)."""
    new_row = [segment, pipeline, status, _now(), notes]
    updated = _upsert_row(
        segments_ws, key_columns=[0, 1], new_row=new_row, key_values=[segment, pipeline]
    )
    verb = "Updated" if updated else "Added"
    return f"{verb} segment '{segment}' ({pipeline}) → status: {status}."


def add_or_update_brief(
    company: str, vertical: str, pipeline: str, status: str, details: str = ""
) -> str:
    """Add or update a brief row. Keyed by (Company, Pipeline)."""
    new_row = [company, vertical, pipeline, status, details, _now()]
    updated = _upsert_row(
        briefs_ws, key_columns=[0, 2], new_row=new_row, key_values=[company, pipeline]
    )
    verb = "Updated" if updated else "Added"
    return f"{verb} brief for '{company}' ({pipeline}) → status: {status}."


def read_state() -> str:
    """Return the full contents of both tabs as JSON, for Claude to read."""
    state = {
        "segments": segments_ws.get_all_values(),
        "briefs": briefs_ws.get_all_values(),
    }
    return json.dumps(state)


# Tool definitions sent to Claude. The descriptions tell Claude when to use each.
TOOLS = [
    {
        "name": "update_segment",
        "description": (
            "Add or update a target vertical/segment in the Segments tab of the "
            "source-of-truth sheet. Use when the user adds, pauses, activates, "
            "or changes a segment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "segment": {
                    "type": "string",
                    "description": "Segment / vertical name, e.g. 'mobile gaming'.",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Which pipeline: 'advertiser', 'publisher', or 'both'.",
                },
                "status": {
                    "type": "string",
                    "description": "New status, e.g. 'active' or 'paused'.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional extra detail to record.",
                },
            },
            "required": ["segment", "pipeline", "status"],
        },
    },
    {
        "name": "add_or_update_brief",
        "description": (
            "Add or update a campaign brief in the Briefs tab. Use when the user "
            "adds a new campaign brief, marks one discontinued, or changes its "
            "details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "Advertiser/company name."},
                "vertical": {
                    "type": "string",
                    "description": "The vertical/industry, if mentioned.",
                },
                "pipeline": {
                    "type": "string",
                    "description": "Which pipeline: 'advertiser', 'publisher', or 'both'.",
                },
                "status": {
                    "type": "string",
                    "description": "e.g. 'active' or 'discontinued'.",
                },
                "details": {
                    "type": "string",
                    "description": "Free-text brief details, if any.",
                },
            },
            "required": ["company", "pipeline", "status"],
        },
    },
    {
        "name": "read_state",
        "description": (
            "Read the current contents of both the Segments and Briefs tabs. You "
            "have NO memory of your own, so you MUST call this FIRST for ANY "
            "question about the current state — what is active, paused, "
            "discontinued, or recorded, or 'do we have X' — before you answer. "
            "Never claim you have no record or no memory without calling this "
            "first."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def _run_tool(name: str, tool_input: dict) -> str:
    """Execute a tool Claude asked for and return a text result."""
    if name == "update_segment":
        return update_segment(
            tool_input["segment"],
            tool_input["pipeline"],
            tool_input["status"],
            tool_input.get("notes", ""),
        )
    if name == "add_or_update_brief":
        return add_or_update_brief(
            tool_input["company"],
            tool_input.get("vertical", ""),
            tool_input["pipeline"],
            tool_input["status"],
            tool_input.get("details", ""),
        )
    if name == "read_state":
        return read_state()
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Talking to Claude (with tool calling)
# ---------------------------------------------------------------------------

# Safety cap so a tool loop can never run forever.
MAX_TOOL_ROUNDS = 6


def ask_claude(user_text: str) -> str:
    """
    Get a reply from Claude. Claude may call our sheet tools one or more times;
    we run each tool, feed the result back, and repeat until Claude gives a
    final text answer. Falls back to the latest model if the model string is
    rejected.
    """
    global active_model
    messages = [{"role": "user", "content": user_text}]

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = claude.messages.create(
                model=active_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
        except (anthropic.NotFoundError, anthropic.BadRequestError) as error:
            if active_model != FALLBACK_MODEL:
                log.warning(
                    "Model %r was rejected (%s). Falling back to %r.",
                    active_model,
                    error.__class__.__name__,
                    FALLBACK_MODEL,
                )
                active_model = FALLBACK_MODEL
                continue
            raise

        if response.stop_reason != "tool_use":
            # Claude is done — return its text answer.
            return "".join(b.text for b in response.content if b.type == "text")

        # Claude wants to use one or more tools. Record its turn, run the tools,
        # then send the results back so it can continue.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = _run_tool(block.name, block.input)
                is_error = False
            except Exception as exc:  # noqa: BLE001 — report any tool failure to Claude
                log.exception("Tool %s failed", block.name)
                result = f"Error running {block.name}: {exc}"
                is_error = True
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

    return "Sorry, I couldn't finish that — too many steps. Please try rephrasing."


# ---------------------------------------------------------------------------
# Slack event handling
# ---------------------------------------------------------------------------


def _reply_with_claude(user_text, event, say, logger):
    """Shared logic: send text to Claude and post the reply in a thread."""
    user_text = user_text.strip()
    if not user_text:
        return

    # Reply inside a thread: if the message is already in a thread use that
    # thread; otherwise start a thread on the original message.
    thread_ts = event.get("thread_ts") or event["ts"]

    try:
        reply = ask_claude(user_text)
    except Exception:
        logger.exception("Failed to get a reply from Claude")
        say(
            text="Sorry, I hit an error reaching Claude. Please try again.",
            thread_ts=thread_ts,
        )
        return

    say(text=reply or "(no reply)", thread_ts=thread_ts)


@app.event("app_mention")
def handle_app_mention(event, say, logger):
    """
    Runs when someone @mentions the bot in a channel. We remove the bot's own
    @mention from the text and send the rest to Claude.
    """
    if event.get("bot_id"):
        return
    user_text = strip_bot_mention(event.get("text", ""))
    _reply_with_claude(user_text, event, say, logger)


@app.event("message")
def handle_message(event, say, logger):
    """
    Runs every time a message is posted in a channel the bot is in. This lets
    you talk to the bot WITHOUT @mentioning it.

    We ignore:
      * messages from any bot (event has a "bot_id"), which also covers our own
        messages, so the bot never replies to itself;
      * message edits, deletions, joins, and other non-standard message types
        (these have a "subtype");
      * messages that @mention the bot — those are handled by the app_mention
        handler above, so skipping them here avoids replying twice.
    """
    if event.get("bot_id") or event.get("subtype"):
        return

    user_text = event.get("text", "")
    if mentions_bot(user_text):
        return

    _reply_with_claude(user_text, event, say, logger)


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Agent 1 (preferred model: %s)…", PREFERRED_MODEL)
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
