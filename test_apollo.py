"""
test_apollo.py — a tiny, credit-cheap capability test for the Apollo.io API.

What it does (and what it costs in Apollo credits):
  1. People Search  -> finds up to 5 people matching a sample ICP.
                       This endpoint does NOT consume credits and does NOT
                       return email addresses.
  2. People Match   -> ONE enrichment call on the first person found, to see
                       whether Apollo will reveal a verified work email.
                       This is the only step that can consume a credit (~1).

Total expected credit usage: 0 for search + ~1 for the single enrichment = ~1.

Run it with:   python3 test_apollo.py
"""

import os
import json

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
load_dotenv()  # reads the .env file in this folder

API_KEY = os.getenv("APOLLO_API_KEY", "").strip()

# Apollo authenticates with the "X-Api-Key" HTTP header (per Apollo's docs).
HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "X-Api-Key": API_KEY,
}

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"

# A small sample of the ICP we want to test against.
ICP_TITLES = [
    "Growth Marketing Manager",
    "User Acquisition Manager",
    "Performance Marketing Manager",
    "Head of Digital Marketing",
]
ICP_LOCATION = "India"
RESULT_LIMIT = 5


def line(char="-", n=70):
    print(char * n)


def show_rate_and_credit_info(response):
    """Print any rate-limit / credit-usage info Apollo returns in the headers."""
    interesting = []
    for key, value in response.headers.items():
        k = key.lower()
        if any(word in k for word in ("rate", "limit", "credit", "minute", "hour", "day", "request")):
            interesting.append(f"    {key}: {value}")
    if interesting:
        print("  Apollo rate-limit / usage headers:")
        print("\n".join(interesting))
    else:
        print("  (Apollo returned no rate-limit/credit headers on this response.)")


def check_key():
    if not API_KEY or API_KEY == "PASTE_YOUR_APOLLO_KEY_HERE":
        print("ERROR: No Apollo API key found.")
        print("Open the .env file in this folder and paste your key after")
        print("APOLLO_API_KEY=  then run this script again.")
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Step 1: People Search (free — no credits, no emails)
# ---------------------------------------------------------------------------
def people_search():
    line("=")
    print("STEP 1: PEOPLE SEARCH  (does not use credits, does not return emails)")
    line("=")
    print(f"  Titles   : {ICP_TITLES}")
    print(f"  Location : {ICP_LOCATION}")
    print(f"  Limit    : {RESULT_LIMIT} results")
    print()

    payload = {
        "person_titles": ICP_TITLES,
        "person_locations": [ICP_LOCATION],
        "page": 1,
        "per_page": RESULT_LIMIT,
    }

    resp = requests.post(SEARCH_URL, headers=HEADERS, json=payload, timeout=30)
    print(f"  HTTP status: {resp.status_code}")
    show_rate_and_credit_info(resp)
    print()

    if resp.status_code != 200:
        print("  Search did NOT succeed. Apollo's response body:")
        print("   ", resp.text[:1500])
        explain_error(resp.status_code)
        return None

    data = resp.json()
    people = data.get("people", []) or data.get("contacts", [])

    # Apollo returns the total match count at the TOP LEVEL as "total_entries".
    total = data.get("total_entries", "unknown")
    print(f"  >>> TOTAL matching people Apollo reports: {total}")
    print(f"  >>> People returned on this page: {len(people)}")
    print()

    for i, p in enumerate(people, start=1):
        # Search hides the last name (last_name_obfuscated) and the LinkedIn URL.
        last = p.get("last_name") or p.get("last_name_obfuscated") or ""
        name = (p.get("name") or f"{p.get('first_name','')} {last}").strip()
        title = p.get("title", "(no title)")
        org = p.get("organization", {}) or {}
        company = org.get("name") or p.get("organization_name") or "(no company)"
        linkedin = p.get("linkedin_url") or "(not shown in search)"

        # Search never returns the email itself, only a has_email flag.
        email = p.get("email")
        if email:
            email_status = f"included: {email}"
        elif p.get("has_email") is True:
            email_status = "LOCKED — an email exists but is hidden (reveal via enrichment)"
        else:
            email_status = "no email on file / not shown"

        print(f"  [{i}] {name}")
        print(f"      Title    : {title}")
        print(f"      Company  : {company}")
        print(f"      LinkedIn : {linkedin}")
        print(f"      Email    : {email_status}")
        print()

    return people[0] if people else None


# ---------------------------------------------------------------------------
# Step 2: People Match / Enrichment (ONE call — may use ~1 credit)
# ---------------------------------------------------------------------------
def people_match(person):
    line("=")
    print("STEP 2: PEOPLE ENRICHMENT / MATCH  (ONE call — this may use ~1 credit)")
    line("=")

    if not person:
        print("  No person from search to enrich — skipping (0 credits used).")
        return

    name = person.get("name") or f"{person.get('first_name','')} {person.get('last_name','')}".strip()
    print(f"  Enriching the FIRST person only: {name}")
    print()

    org = person.get("organization", {}) or {}
    payload = {
        "id": person.get("id"),
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "name": person.get("name"),
        "organization_name": org.get("name") or person.get("organization_name"),
        "domain": org.get("primary_domain") or org.get("website_url"),
        "linkedin_url": person.get("linkedin_url"),
        # We do NOT set reveal_personal_emails or reveal_phone_number,
        # to avoid spending extra credits. We only check for a work email.
    }
    # Drop empty fields so we send a clean request.
    payload = {k: v for k, v in payload.items() if v}

    resp = requests.post(MATCH_URL, headers=HEADERS, json=payload, timeout=30)
    print(f"  HTTP status: {resp.status_code}")
    show_rate_and_credit_info(resp)
    print()

    if resp.status_code != 200:
        print("  Enrichment did NOT succeed. Apollo's response body:")
        print("   ", resp.text[:1500])
        explain_error(resp.status_code)
        return

    data = resp.json()
    matched = data.get("person") or {}

    email = matched.get("email")
    email_status = matched.get("email_status")  # e.g. "verified", "guessed", None

    print("  --- What enrichment returned ---")
    print(f"  Name         : {matched.get('name')}")
    print(f"  Title        : {matched.get('title')}")
    company = (matched.get('organization') or {}).get('name')
    print(f"  Company      : {company}")
    print(f"  LinkedIn     : {matched.get('linkedin_url')}")
    print(f"  Email        : {email or 'NOT returned'}")
    print(f"  Email status : {email_status or 'n/a'}  (Apollo marks verified emails as 'verified')")
    print()
    print("  (Full 'person' object below, trimmed to keep it readable.)")
    trimmed = {k: matched.get(k) for k in (
        "id", "name", "title", "email", "email_status",
        "linkedin_url", "city", "state", "country",
    )}
    print(json.dumps(trimmed, indent=2))


def explain_error(status):
    msgs = {
        401: "401 = your API key was rejected (wrong or missing key).",
        403: "403 = your plan is not allowed to use this endpoint. On free/basic\n"
             "      plans some endpoints require a paid tier or a 'master' API key.",
        422: "422 = Apollo did not like the request parameters.",
        429: "429 = rate limit / out of credits for now. Try again later.",
    }
    if status in msgs:
        print("  NOTE:", msgs[status])


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    check_key()
    first_person = people_search()
    people_match(first_person)
    line("=")
    print("Done. Search uses 0 credits; the single enrichment call uses ~1 if it")
    print("returned an email. Total expected: ~1 credit.")
    line("=")
