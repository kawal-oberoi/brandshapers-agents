"""
Agent 1 — a Slack bot for Brand Shapers.

What it does:
  * Connects to Slack using Socket Mode (no public URL or webhooks needed).
  * Listens for messages in any channel the bot has been added to.
  * When a HUMAN posts a message, it sends that text to Claude and posts
    Claude's reply back as a threaded reply in the same channel.
  * It ignores messages from bots and from itself, so it never replies to
    its own messages.

You do not need to edit this file to use the bot. The three secret tokens are
read from environment variables (loaded from a local .env file in development).
"""

import logging
import os

import anthropic
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

# Load SLACK_BOT_TOKEN, SLACK_APP_TOKEN, and ANTHROPIC_API_KEY from a local
# .env file when running on your own machine. (In production you would set
# real environment variables instead.)
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("agent-1")

# The model we want to use, plus the fallback if that exact string ever stops
# being available. claude-opus-4-8 is the latest, most capable Claude model.
PREFERRED_MODEL = "claude-sonnet-4-6"
FALLBACK_MODEL = "claude-opus-4-8"

# Once we discover which model actually works, we remember it so we don't keep
# retrying the unavailable one on every message.
active_model = PREFERRED_MODEL

SYSTEM_PROMPT = (
    "You are Agent 1, the operations brain for Brand Shapers, a performance "
    "and affiliate marketing agency. The user (Kawalpreet) talks to you in "
    "plain English about LinkedIn outreach operations — e.g. adding or pausing "
    "a target vertical or segment, adding a new campaign brief, marking a "
    "campaign as discontinued, or changing the ideal customer profile. Your "
    "job: understand the intent, restate it back as a short structured summary "
    "(what changed, which pipeline it affects — advertiser or publisher — and "
    "any details you captured), and ask one concise clarifying question only if "
    "something essential is missing. Keep replies short and businesslike."
)

# Read the three secrets. We fail fast with a clear message if any are missing,
# so you immediately know what to fix.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

_missing = [
    name
    for name, value in (
        ("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN),
        ("SLACK_APP_TOKEN", SLACK_APP_TOKEN),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    )
    if not value
]
if _missing:
    raise SystemExit(
        "Missing required environment variable(s): "
        + ", ".join(_missing)
        + ".\nCreate a .env file (copy .env.example) and fill in your tokens."
    )

app = App(token=SLACK_BOT_TOKEN)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Talking to Claude
# ---------------------------------------------------------------------------


def _call_claude(model: str, user_text: str) -> str:
    """Send one message to Claude and return its reply text."""
    response = claude.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_text}],
    )
    # The response is a list of content blocks; collect the text from each one.
    return "".join(block.text for block in response.content if block.type == "text")


def ask_claude(user_text: str) -> str:
    """
    Get a reply from Claude using the preferred model, falling back to the
    latest model if the preferred model string is rejected.
    """
    global active_model
    try:
        return _call_claude(active_model, user_text)
    except (anthropic.NotFoundError, anthropic.BadRequestError) as error:
        # The model string was rejected. If we haven't already switched to the
        # fallback, switch now and tell the operator in the logs.
        if active_model != FALLBACK_MODEL:
            log.warning(
                "Model %r was rejected (%s). Falling back to %r.",
                active_model,
                error.__class__.__name__,
                FALLBACK_MODEL,
            )
            active_model = FALLBACK_MODEL
            return _call_claude(active_model, user_text)
        raise


# ---------------------------------------------------------------------------
# Slack event handling
# ---------------------------------------------------------------------------


@app.event("message")
def handle_message(event, say, logger):
    """
    Runs every time a message is posted in a channel the bot is in.

    We ignore:
      * messages from any bot (event has a "bot_id"), which also covers our own
        messages, so the bot never replies to itself;
      * message edits, deletions, joins, and other non-standard message types
        (these have a "subtype").
    """
    if event.get("bot_id") or event.get("subtype"):
        return

    user_text = event.get("text", "").strip()
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


# ---------------------------------------------------------------------------
# Start the bot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("Starting Agent 1 (preferred model: %s)…", PREFERRED_MODEL)
    SocketModeHandler(app, SLACK_APP_TOKEN).start()
