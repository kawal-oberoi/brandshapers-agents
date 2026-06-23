"""
apollo.py — the reusable Apollo.io client behind Agent 2 (the Sourcer).

This wraps the two endpoints we tested and confirmed on 2026-06-23:

  search_people(...)  -> FREE (0 credits). Returns each person's title, company,
                         location, a MASKED name, an Apollo person id, and a
                         has_email flag. It NEVER returns a LinkedIn URL or email.

  enrich_person(...)  -> ~1 credit per call. Returns the full name, LinkedIn URL,
                         and a verified work email.

Design rule for protecting credits: search and score for FREE, then enrich ONLY
the qualified keepers. All errors raise ApolloError so the caller can post a
clear Slack message instead of crashing.
"""

import os

import requests

SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
MATCH_URL = "https://api.apollo.io/api/v1/people/match"


class ApolloError(Exception):
    """A reportable Apollo failure (bad key, blocked plan, rate limit, etc.)."""

    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def _headers():
    # Apollo authenticates with the "X-Api-Key" header (per Apollo's docs).
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": os.environ.get("APOLLO_API_KEY", "").strip(),
    }


def _friendly_error(status, body):
    msgs = {
        401: "Apollo rejected the API key (401). Check APOLLO_API_KEY in your .env / Railway.",
        403: "Apollo blocked this request (403). Your plan may not allow this endpoint.",
        422: "Apollo didn't accept the search filters (422).",
        429: "Apollo rate limit reached or out of credits for now (429). Try again later.",
    }
    return msgs.get(status, f"Apollo returned HTTP {status}: {str(body)[:300]}")


def search_people(titles, locations, organization_keywords=None, page=1, per_page=25):
    """
    FREE People Search. Returns (people, total_entries):
      * people        — list of person dicts (masked name, title, org, id, has_email)
      * total_entries — how many people Apollo says match in total
    Raises ApolloError on any non-200 response or missing API key.
    """
    if not os.environ.get("APOLLO_API_KEY", "").strip():
        raise ApolloError(0, "No APOLLO_API_KEY is set in the environment / .env file.")

    payload = {
        "person_titles": titles,
        "person_locations": locations,
        "page": page,
        "per_page": per_page,
    }
    # q_organization_keyword_tags filters by the employer's keywords/industry.
    # (Verified live: e.g. "insurance" narrows India marketers to ~8.9k.)
    if organization_keywords:
        payload["q_organization_keyword_tags"] = organization_keywords

    try:
        resp = requests.post(SEARCH_URL, headers=_headers(), json=payload, timeout=30)
    except requests.RequestException as exc:
        raise ApolloError(0, f"Could not reach Apollo (network error): {exc}") from exc

    if resp.status_code != 200:
        raise ApolloError(resp.status_code, _friendly_error(resp.status_code, resp.text))

    data = resp.json()
    return (data.get("people", []) or []), data.get("total_entries", 0)


def enrich_person(person):
    """
    ~1 CREDIT People Match on a single person from search results. Returns the
    matched person dict (full name, linkedin_url, email, email_status, location).
    Raises ApolloError on any non-200 response. We do NOT request personal emails
    or phone numbers, to avoid spending extra credits.
    """
    org = person.get("organization", {}) or {}
    payload = {
        "id": person.get("id"),
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "name": person.get("name"),
        "organization_name": org.get("name") or person.get("organization_name"),
        "domain": org.get("primary_domain") or org.get("website_url"),
        "linkedin_url": person.get("linkedin_url"),
    }
    payload = {k: v for k, v in payload.items() if v}  # drop empty fields

    try:
        resp = requests.post(MATCH_URL, headers=_headers(), json=payload, timeout=30)
    except requests.RequestException as exc:
        raise ApolloError(0, f"Could not reach Apollo (network error): {exc}") from exc

    if resp.status_code != 200:
        raise ApolloError(resp.status_code, _friendly_error(resp.status_code, resp.text))

    return resp.json().get("person") or {}
