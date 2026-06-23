# Agent 1 — Brand Shapers Slack bot

A Slack bot that listens in channels it belongs to. When a person posts a
message, the text is sent to Claude (Anthropic) and Claude's reply is posted
back as a threaded reply. It ignores messages from bots and from itself.

It uses **Socket Mode**, so it does **not** need a public URL or webhooks — it
runs from your laptop (or any server) and connects out to Slack.

## What you need

- Python 3.9 or newer
- A Slack app (with a Bot Token and an App-Level Token)
- An Anthropic API key

## One-time setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install the dependencies
pip install -r requirements.txt

# 3. Create your secrets file from the template and fill in real values
cp .env.example .env
# then open .env in a text editor and paste in your three tokens
```

## Run the bot

```bash
source .venv/bin/activate   # if not already active
python app.py
```

You should see `Starting Agent 1…`. Now invite the bot to a Slack channel
(`/invite @Agent 1`) and post a message — it will reply in a thread.

Stop the bot with `Ctrl + C`.

## The three secrets

These are read from environment variables, loaded from `.env` in development:

| Variable            | Where it comes from                                   |
| ------------------- | ----------------------------------------------------- |
| `SLACK_BOT_TOKEN`   | Slack app → OAuth & Permissions → Bot User OAuth Token (`xoxb-…`) |
| `SLACK_APP_TOKEN`   | Slack app → Basic Information → App-Level Tokens (`xapp-…`, scope `connections:write`) |
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys (`sk-ant-…`)         |

## Agent 2 — the Sourcer

Agent 2 finds new outreach leads from Apollo.io and writes them into a **Leads**
tab in the same Google Sheet. It is built to protect Apollo credits above all:

- **Search is free.** It pulls candidates and scores them A/B/C using only free
  Apollo search data (title, company, location, a masked name, and a
  "has email?" flag).
- **Enrichment costs ~1 credit each**, so it enriches **only** the best Tier-A
  then Tier-B keepers, never weak (C) matches or competitor agencies.
- **Monthly cap** (`APOLLO_MONTHLY_CAP`, default 70) is a hard ceiling — it
  trims or stops a run before it can ever overspend, and resets each month.
- **Dry runs cost nothing.** `dry_run=true` does everything except enrichment.

**Use it from Slack** by talking to the bot in plain English, e.g.
*"source 10 bfsi-lending advertiser leads"* or *"find publisher leads"*. The
detailed summary (searched / qualified / enriched / written / credits used) is
posted to **#outreach-control**.

**Try a free preview from the terminal** (no credits, no Slack needed):

```bash
python3 sourcer.py bfsi-lending      # preview the default 10
python3 sourcer.py publisher 5       # preview 5 publisher leads
```

Segments live in `sourcer.py` under `SEARCH_PRESETS` and are easy to edit:
`bfsi-insurance`, `bfsi-lending`, `bfsi-neobank`, `bfsi-stocktrading`,
`mobile-gaming`, `ott`, `dating` (advertiser) and `publisher`.

A built-in **weekly scheduler** auto-sources the next active advertiser segment
in rotation (default Monday 09:00 India time). Turn it off or change the timing
with the `SOURCER_SCHEDULE_*` environment variables (see `.env.example`).

## Model

The bot uses `claude-opus-4-8`, the latest and most capable Claude model.

## Deploying 24/7 (Railway)

This repo is ready to run on [Railway](https://railway.app) as a background
**worker** — no web server or open port is needed, because Slack Socket Mode
connects outward.

- `Procfile` defines a single `worker` process that runs `python app.py`.
- `.python-version` pins the Python version Railway should use.
- The three secrets are set in **Railway → your project → Variables**, never
  committed to git: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ANTHROPIC_API_KEY`.

After connecting this GitHub repo to Railway, Railway installs
`requirements.txt` and starts the worker automatically.
