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
from zoneinfo import ZoneInfo

import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import sheets    # shared Google Sheet connection (used by both agents)
import sourcer   # Agent 2 — the Sourcer

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
    "You may be shown the last few messages of the current Slack conversation "
    "for immediate context — use them ONLY to follow a multi-turn exchange "
    "(e.g. to interpret a short follow-up answer to a question you just asked). "
    "You have NO durable memory of actual operational state: a Google Sheet is "
    "your ONE AND ONLY source of truth for what is active, paused, "
    "discontinued, or recorded, and the tools below are the ONLY way to read or "
    "change it. Never rely on the conversation for current state — always "
    "confirm it via read_state.\n\n"
    "  - read_state: returns the current contents of both the Segments and "
    "Briefs tabs.\n"
    "  - update_segment: add or update a target vertical/segment in the "
    "Segments tab (e.g. pause or activate 'mobile gaming').\n"
    "  - add_or_update_brief: add or update a campaign brief in the Briefs tab "
    "(e.g. add a new brief or mark one discontinued).\n"
    "  - source_leads: find new outreach leads for a segment (this is Agent 2, "
    "the Sourcer). Use it whenever the user asks to source, find, pull, or get "
    "leads/prospects — e.g. 'source 10 bfsi-lending advertiser leads' or 'find "
    "publisher leads'. Map their words to one segment key and pass max_enrich if "
    "they give a number. Only set dry_run=true if they ask for a test/preview.\n\n"
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
    "— both pipelines.'\n"
    "4. ACT, DON'T OVER-ASK. When an instruction to pause, activate, add, "
    "discontinue, or change something is clear and names the pipeline (or "
    "clearly applies to 'both'), record it RIGHT AWAY with the correct write "
    "tool and confirm — do NOT ask for confirmation or an effective date. "
    "Changes take effect immediately when recorded; only note a future date if "
    "the user volunteers one. Ask a SINGLE clarifying question only when "
    "essential information is genuinely missing — for example, the pipeline was "
    "not given and cannot be inferred. 'Pause mobile gaming, both pipelines' is "
    "complete: call update_segment immediately, then confirm.\n\n"
    "Pipelines are 'advertiser', 'publisher', or 'both'. When you restate "
    "intent, give a short structured summary (what changed, which pipeline, key "
    "details). Never let a missing detail stop you from calling read_state for "
    "a state question."
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
# Google Sheet — the source of truth (the connection lives in sheets.py and is
# shared with Agent 2, so both agents read/write the same one Sheet).
# ---------------------------------------------------------------------------

# Tab names and their header rows.
SEGMENTS_TAB = "Segments"
SEGMENTS_HEADERS = ["Segment", "Pipeline", "Status", "Updated", "Notes"]
BRIEFS_TAB = "Briefs"
BRIEFS_HEADERS = ["Company", "Vertical", "Pipeline", "Status", "Details", "Updated"]

_spreadsheet = sheets.open_spreadsheet()
if _spreadsheet is None:
    raise SystemExit(
        "No Google credentials / Sheet ID found. Either set GOOGLE_CREDENTIALS_JSON "
        "or put google-credentials.json in the project folder, and set GOOGLE_SHEET_ID."
    )
segments_ws = sheets.ensure_tab(_spreadsheet, SEGMENTS_TAB, SEGMENTS_HEADERS)
briefs_ws = sheets.ensure_tab(_spreadsheet, BRIEFS_TAB, BRIEFS_HEADERS)
log.info("Google Sheet ready (tabs: %s, %s).", SEGMENTS_TAB, BRIEFS_TAB)


# ---------------------------------------------------------------------------
# Tools Claude can call
# ---------------------------------------------------------------------------


def update_segment(segment: str, pipeline: str, status: str, notes: str = "") -> str:
    """Add or update a segment row. Keyed by (Segment, Pipeline)."""
    new_row = [segment, pipeline, status, sheets.now_utc(), notes]
    updated = sheets.upsert_row(
        segments_ws, key_columns=[0, 1], new_row=new_row, key_values=[segment, pipeline]
    )
    verb = "Updated" if updated else "Added"
    return f"{verb} segment '{segment}' ({pipeline}) → status: {status}."


def add_or_update_brief(
    company: str, vertical: str, pipeline: str, status: str, details: str = ""
) -> str:
    """Add or update a brief row. Keyed by (Company, Pipeline)."""
    new_row = [company, vertical, pipeline, status, details, sheets.now_utc()]
    updated = sheets.upsert_row(
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


# Where Agent 2 posts its sourcing summaries. Configurable via env.
OUTREACH_CHANNEL = os.environ.get("OUTREACH_CHANNEL", sourcer.OUTREACH_CHANNEL)


def post_to_outreach(text: str) -> None:
    """Post a message to the #outreach-control channel (used by the Sourcer)."""
    try:
        app.client.chat_postMessage(channel=OUTREACH_CHANNEL, text=text)
    except Exception:
        log.exception("Could not post to %s", OUTREACH_CHANNEL)


def source_leads_tool(segment: str, max_enrich: int = 10, dry_run: bool = False) -> str:
    """
    Run Agent 2's sourcing for a segment. The full plain-English summary is
    posted to #outreach-control; this returns a short confirmation for the
    Slack thread the user typed in.
    """
    summary = sourcer.source_leads(
        segment, max_enrich=int(max_enrich), dry_run=bool(dry_run), notify=post_to_outreach
    )
    if not summary.get("ok"):
        return summary.get("message", "Sourcing could not run — see #outreach-control.")
    if summary.get("skipped"):
        return f"Skipped {summary.get('segment')} ({summary.get('reason')}). Posted a note to {OUTREACH_CHANNEL}."
    seg = summary.get("segment")
    if summary.get("dry_run"):
        return (f"Dry run for {seg} done (0 credits) — would enrich "
                f"{summary.get('would_enrich', 0)}. Details in {OUTREACH_CHANNEL}.")
    return (f"Sourced {seg}: enriched {summary.get('enriched', 0)}, wrote "
            f"{summary.get('written', 0)}, used {summary.get('credits_used', 0)} credits. "
            f"Full summary in {OUTREACH_CHANNEL}.")


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
    {
        "name": "source_leads",
        "description": (
            "Find (source) new outreach leads for a segment using Agent 2, the "
            "Sourcer. Use this when the user asks to 'source', 'find', 'pull', or "
            "'get' leads/prospects — e.g. 'source 10 bfsi-lending advertiser "
            "leads' or 'find publisher leads'. It runs a free Apollo search, "
            "scores candidates, and enriches only the best ones (each enrichment "
            "costs ~1 Apollo credit). Map the user's words to ONE segment key. "
            "Advertiser segments: bfsi-insurance, bfsi-lending, bfsi-neobank, "
            "bfsi-stocktrading, mobile-gaming, ott, dating. Publisher segment: "
            "publisher. If the user only says 'publisher leads', use segment "
            "'publisher'. Set dry_run=true ONLY if the user asks for a test / "
            "preview / dry run (then it spends 0 credits). Default max_enrich is "
            "10. The detailed summary is posted to the outreach channel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "segment": {
                    "type": "string",
                    "description": (
                        "The segment key, e.g. 'bfsi-lending', 'mobile-gaming', "
                        "or 'publisher'."
                    ),
                },
                "max_enrich": {
                    "type": "integer",
                    "description": "Max people to enrich this run (each ~1 credit). Default 10.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, preview only — spend 0 credits (no enrichment).",
                },
            },
            "required": ["segment"],
        },
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
    if name == "source_leads":
        return source_leads_tool(
            tool_input["segment"],
            tool_input.get("max_enrich", 10),
            tool_input.get("dry_run", False),
        )
    return f"Unknown tool: {name}"


# ---------------------------------------------------------------------------
# Talking to Claude (with tool calling)
# ---------------------------------------------------------------------------

# Safety cap so a tool loop can never run forever.
MAX_TOOL_ROUNDS = 6


def ask_claude(messages: list) -> str:
    """
    Get a reply from Claude. `messages` is the conversation so far (recent prior
    turns plus the current message, built by _build_conversation). Claude may
    call our sheet tools one or more times; we run each tool, feed the result
    back, and repeat until Claude gives a final text answer. Falls back to the
    latest model if the model string is rejected.
    """
    global active_model
    messages = list(messages)  # copy: we append tool turns below

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


# How many recent messages to pull from the channel/thread for short-term memory
# of multi-turn exchanges. The Google Sheet stays the source of truth for actual
# state — this is only enough context for follow-up answers to connect.
HISTORY_LIMIT = 12


def _slack_history(event) -> list:
    """
    Fetch the recent conversation around this message so multi-turn exchanges
    connect. If the message is inside a thread we read that thread; otherwise we
    read the channel's recent messages. Returns Slack message dicts in
    chronological (oldest-first) order. On any failure we return an empty list,
    so the bot still answers using just the current message.
    """
    channel = event.get("channel")
    thread_ts = event.get("thread_ts")
    try:
        if thread_ts:
            resp = app.client.conversations_replies(
                channel=channel, ts=thread_ts, limit=HISTORY_LIMIT
            )
        else:
            resp = app.client.conversations_history(
                channel=channel, limit=HISTORY_LIMIT
            )
        messages = resp.get("messages", []) or []
    except Exception:
        log.exception("Could not fetch Slack history; answering without it.")
        return []
    # conversations.history returns newest-first; replies returns oldest-first.
    # Sort by timestamp so we always feed Claude the turns in chronological order.
    messages.sort(key=lambda m: m.get("ts", "0"))
    return messages


def _build_conversation(event, user_text: str) -> list:
    """
    Turn the recent Slack messages into Claude-style prior turns, then append the
    current message as the final user turn.

      * The bot's own past messages become 'assistant' turns, so a follow-up like
        'effective immediately' connects to the bot's own clarifying question.
      * Everyone else's messages become 'user' turns.
      * The current message (matched by ts) is excluded from the history scan and
        re-added last, so it is always present and always last.
      * Consecutive same-role turns are merged, and any leading assistant turns
        are dropped (the API requires the conversation to start with the user).
    """
    current_ts = event.get("ts")
    turns = []
    for msg in _slack_history(event):
        if msg.get("ts") == current_ts:
            continue  # the current message is appended explicitly below
        if msg.get("subtype"):
            continue  # channel joins, edits, and other non-message events
        text = strip_bot_mention(msg.get("text", "")).strip()
        if not text:
            continue
        is_bot = bool(msg.get("bot_id")) or msg.get("user") == BOT_USER_ID
        role = "assistant" if is_bot else "user"
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"] += "\n" + text
        else:
            turns.append({"role": role, "content": text})

    # Claude requires the conversation to start with a user turn.
    while turns and turns[0]["role"] == "assistant":
        turns.pop(0)

    # Append the current message as the final user turn (merge if the previous
    # turn was also the user, so roles stay clean).
    if turns and turns[-1]["role"] == "user":
        turns[-1]["content"] += "\n" + user_text
    else:
        turns.append({"role": "user", "content": user_text})
    return turns


def _reply_with_claude(user_text, event, say, logger):
    """Shared logic: send text to Claude and post the reply in a thread."""
    user_text = user_text.strip()
    if not user_text:
        return

    # Reply inside a thread: if the message is already in a thread use that
    # thread; otherwise start a thread on the original message.
    thread_ts = event.get("thread_ts") or event["ts"]

    # Include recent conversation as prior turns so follow-up answers connect.
    messages = _build_conversation(event, user_text)

    try:
        reply = ask_claude(messages)
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
# Weekly scheduler (Agent 2) — auto-source the next active advertiser segment.
# All settings are env vars so you can change day/time or switch it off without
# touching code. Defaults: ON, every Monday 09:00 India time.
# ---------------------------------------------------------------------------
SCHEDULE_ENABLED = os.environ.get("SOURCER_SCHEDULE_ENABLED", "true").lower() in (
    "1", "true", "yes", "on",
)
SCHEDULE_DAY = os.environ.get("SOURCER_SCHEDULE_DAY", "mon")          # mon..sun
SCHEDULE_HOUR = int(os.environ.get("SOURCER_SCHEDULE_HOUR", "9"))     # 0..23
SCHEDULE_MINUTE = int(os.environ.get("SOURCER_SCHEDULE_MINUTE", "0"))
SCHEDULE_TZ = os.environ.get("SOURCER_SCHEDULE_TZ", "Asia/Kolkata")
SCHEDULE_MAX_ENRICH = int(os.environ.get("SOURCER_SCHEDULE_MAX_ENRICH", "10"))


def start_scheduler():
    """Start the weekly auto-sourcing job, unless it's switched off."""
    if not SCHEDULE_ENABLED:
        log.info("Weekly sourcing scheduler is OFF (SOURCER_SCHEDULE_ENABLED).")
        return
    scheduler = BackgroundScheduler(timezone=ZoneInfo(SCHEDULE_TZ))
    scheduler.add_job(
        lambda: sourcer.run_scheduled(
            notify=post_to_outreach, max_enrich=SCHEDULE_MAX_ENRICH
        ),
        trigger="cron",
        day_of_week=SCHEDULE_DAY,
        hour=SCHEDULE_HOUR,
        minute=SCHEDULE_MINUTE,
        id="weekly_sourcing",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info(
        "Weekly sourcing scheduled: %s %02d:%02d %s (max_enrich=%d).",
        SCHEDULE_DAY, SCHEDULE_HOUR, SCHEDULE_MINUTE, SCHEDULE_TZ, SCHEDULE_MAX_ENRICH,
    )


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Agent 1 + Agent 2 (preferred model: %s)…", PREFERRED_MODEL)
    start_scheduler()
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
