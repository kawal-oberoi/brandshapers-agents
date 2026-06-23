"""
sheets.py — the shared Google Sheet connection used by every agent.

Both Agent 1 (the operations brain in app.py) and Agent 2 (the Sourcer in
sourcer.py) use the SAME Google Sheet as their source of truth, so the
connection and the little helper functions live here in one place.

Nothing here needs editing day to day. Secrets come from environment variables
(loaded from a local .env file in development), exactly as before:
  * GOOGLE_SHEET_ID         — the long id from the sheet's URL.
  * GOOGLE_CREDENTIALS_JSON  — the service-account JSON (used on Railway), OR
  * google-credentials.json  — the same JSON as a local file (used on a laptop).

If credentials or the Sheet ID are missing, open_spreadsheet() returns None
instead of crashing, so tools that can run without the sheet (e.g. a local
Apollo dry run) still work and can say "no sheet connected" clearly.
"""

import json
import logging
import os
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("sheets")

# We only need access to spreadsheets. The Sheet must be shared (as Editor) with
# the service account's email address.
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# The opened spreadsheet is cached so we connect once and reuse it everywhere.
_cached_spreadsheet = None
_connection_attempted = False


def load_google_credentials():
    """
    Build Google credentials from either the GOOGLE_CREDENTIALS_JSON environment
    variable (preferred, used on Railway) or a local google-credentials.json
    file. Returns None if neither is available.
    """
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if raw:
        return Credentials.from_service_account_info(json.loads(raw), scopes=GOOGLE_SCOPES)
    if os.path.exists("google-credentials.json"):
        return Credentials.from_service_account_file(
            "google-credentials.json", scopes=GOOGLE_SCOPES
        )
    return None


def open_spreadsheet():
    """
    Open (once) and return the shared spreadsheet, or None if credentials or the
    Sheet ID are missing. The result is cached, so repeat calls are free.
    """
    global _cached_spreadsheet, _connection_attempted
    if _connection_attempted:
        return _cached_spreadsheet
    _connection_attempted = True

    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
    creds = load_google_credentials()
    if not creds or not sheet_id or "your-" in sheet_id:
        log.warning("Google Sheet not connected (missing credentials or GOOGLE_SHEET_ID).")
        return None

    log.info("Connecting to Google Sheet %s…", sheet_id)
    gc = gspread.authorize(creds)
    _cached_spreadsheet = gc.open_by_key(sheet_id)
    log.info("Google Sheet ready.")
    return _cached_spreadsheet


def ensure_tab(spreadsheet, title, headers):
    """Return the named tab, creating it with a header row if it doesn't exist."""
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=200, cols=max(len(headers), 5))
        worksheet.append_row(headers)
        log.info("Created missing tab %r with headers.", title)
        return worksheet
    if not worksheet.row_values(1):
        worksheet.append_row(headers)
        log.info("Added header row to existing empty tab %r.", title)
    return worksheet


def upsert_row(worksheet, key_columns, new_row, key_values):
    """
    Add or update a row. If a row already exists whose values in `key_columns`
    (0-based indexes) match `key_values` (case-insensitively), overwrite it;
    otherwise append a new row. Returns True if an existing row was updated.
    """
    existing_rows = worksheet.get_all_values()  # includes the header row
    wanted = [str(v).strip().lower() for v in key_values]
    for i in range(1, len(existing_rows)):
        row = existing_rows[i]
        actual = [(row[c].strip().lower() if c < len(row) else "") for c in key_columns]
        if actual == wanted:
            sheet_row = i + 1  # spreadsheet rows are 1-based
            last_col = chr(ord("A") + len(new_row) - 1)
            worksheet.update(range_name=f"A{sheet_row}:{last_col}{sheet_row}", values=[new_row])
            return True
    worksheet.append_row(new_row)
    return False


def now_utc():
    """A readable UTC timestamp for 'Updated'/'DateAdded' columns."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
