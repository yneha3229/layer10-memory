"""
fetch_data.py
-------------
Downloads issues, comments, and events from a public GitHub repo.
Saves everything as raw JSON files locally.

Usage:
    python fetch_data.py

Requirements:
    pip install requests python-dotenv pydantic
"""

import os
import json
import time
import requests
from dotenv import load_dotenv
from pathlib import Path

# ── Load environment variables from .env file ──────────────────────────────
load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER   = os.getenv("REPO_OWNER", "tiangolo")
REPO_NAME    = os.getenv("REPO_NAME",  "fastapi")

if not GITHUB_TOKEN:
    raise ValueError("GITHUB_TOKEN not found. Please add it to your .env file.")

# ── Constants ──────────────────────────────────────────────────────────────
BASE_URL   = "https://api.github.com"
HEADERS    = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

# How many issues to fetch (keep low to start, raise later)
# GitHub returns 100 per page max. 5 pages = 500 issues.
MAX_PAGES  = 20
PER_PAGE   = 100

# Output directories
DATA_DIR     = Path("data")
ISSUES_DIR   = DATA_DIR / "issues"
COMMENTS_DIR = DATA_DIR / "comments"
EVENTS_DIR   = DATA_DIR / "events"
RAW_DIR      = DATA_DIR / "raw"

# Create all folders if they don't exist
for folder in [ISSUES_DIR, COMMENTS_DIR, EVENTS_DIR, RAW_DIR]:
    folder.mkdir(parents=True, exist_ok=True)


# ── Helper: safe API request with rate-limit handling ─────────────────────
def github_get(url: str, params: dict = None) -> dict | list | None:
    for attempt in range(3):  # retry up to 3 times
        try:
            response = requests.get(url, headers=HEADERS, params=params, timeout=30)

            if response.status_code == 403:
                reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait_seconds = max(reset_time - int(time.time()), 1) + 5
                print(f"  ⚠️  Rate limited. Waiting {wait_seconds}s ...")
                time.sleep(wait_seconds)
                continue

            if response.status_code == 200:
                return response.json()

            print(f"  ❌ Error {response.status_code} for {url}")
            return None

        except requests.exceptions.Timeout:
            print(f"  ⏱️  Timeout on attempt {attempt+1}/3, retrying...")
            time.sleep(5)
        except requests.exceptions.ConnectionError:
            print(f"  🔌 Connection error on attempt {attempt+1}/3, retrying...")
            time.sleep(10)

    print(f"  ❌ Failed after 3 attempts: {url}")
    return None


# ── Helper: save JSON to file ──────────────────────────────────────────────
def save_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Step 1: Fetch all issues (open + closed) ───────────────────────────────
def fetch_issues() -> list[dict]:
    """
    Fetches issues from the repo across multiple pages.
    Includes both open and closed issues.
    Saves each page as raw JSON and each issue individually.
    """
    print(f"\n📥 Fetching issues from {REPO_OWNER}/{REPO_NAME} ...")
    all_issues = []

    for page in range(1, MAX_PAGES + 1):
        print(f"  Page {page}/{MAX_PAGES} ...", end=" ")

        url = f"{BASE_URL}/repos/{REPO_OWNER}/{REPO_NAME}/issues"
        params = {
            "state":    "all",       # open + closed
            "per_page": PER_PAGE,
            "page":     page,
            "sort":     "created",
            "direction":"asc",
        }

        issues = github_get(url, params)

        if not issues:
            print("no data, stopping.")
            break

        # GitHub includes pull requests in /issues — filter them out
        issues = [i for i in issues if "pull_request" not in i]

        print(f"got {len(issues)} issues.")

        # Save raw page
        save_json(RAW_DIR / f"issues_page_{page}.json", issues)

        all_issues.extend(issues)

        # Small delay to be polite to the API
        time.sleep(0.5)

    # Save each issue individually by its number
    for issue in all_issues:
        issue_number = issue["number"]
        save_json(ISSUES_DIR / f"issue_{issue_number}.json", issue)

    print(f"✅ Total issues fetched: {len(all_issues)}")
    return all_issues


# ── Step 2: Fetch comments for each issue ─────────────────────────────────
def fetch_comments(issues: list[dict]) -> None:
    """
    For each issue, fetches all comments.
    Saves comments per issue as a JSON list.
    """
    print(f"\n💬 Fetching comments for {len(issues)} issues ...")

    for idx, issue in enumerate(issues):
        issue_number = issue["number"]
        comments_count = issue.get("comments", 0)

        # Skip issues with no comments
        if comments_count == 0:
            continue

        out_path = COMMENTS_DIR / f"comments_{issue_number}.json"

        # Skip if already downloaded
        if out_path.exists():
            continue

        print(f"  [{idx+1}/{len(issues)}] Issue #{issue_number} ({comments_count} comments) ...", end=" ")

        url = f"{BASE_URL}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/comments"
        params = {"per_page": 100}

        comments = github_get(url, params)

        if comments:
            save_json(out_path, comments)
            print(f"saved {len(comments)} comments.")
        else:
            print("failed.")

        time.sleep(0.3)  # polite delay

    print("✅ Comments fetched.")


# ── Step 3: Fetch timeline events for each issue ──────────────────────────
def fetch_events(issues: list[dict]) -> None:
    """
    For each issue, fetches the timeline events.
    Events capture: labels added/removed, assignments, state changes (open/close/reopen).
    This is critical for tracking 'used to be true vs currently true'.
    """
    print(f"\n📅 Fetching events for {len(issues)} issues ...")

    for idx, issue in enumerate(issues):
        issue_number = issue["number"]
        out_path = EVENTS_DIR / f"events_{issue_number}.json"

        # Skip if already downloaded
        if out_path.exists():
            continue

        print(f"  [{idx+1}/{len(issues)}] Issue #{issue_number} ...", end=" ")

        url = f"{BASE_URL}/repos/{REPO_OWNER}/{REPO_NAME}/issues/{issue_number}/events"
        params = {"per_page": 100}

        events = github_get(url, params)

        if events:
            save_json(out_path, events)
            print(f"saved {len(events)} events.")
        else:
            print("0 events or failed.")

        time.sleep(0.3)

    print("✅ Events fetched.")


# ── Step 4: Save a manifest of what we downloaded ─────────────────────────
def save_manifest(issues: list[dict]) -> None:
    """
    Saves a summary manifest so we know exactly what we downloaded,
    when, and from where. Important for reproducibility.
    """
    manifest = {
        "repo":          f"{REPO_OWNER}/{REPO_NAME}",
        "source_url":    f"https://github.com/{REPO_OWNER}/{REPO_NAME}",
        "api_base":      BASE_URL,
        "fetched_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_issues":  len(issues),
        "max_pages":     MAX_PAGES,
        "per_page":      PER_PAGE,
        "issue_numbers": [i["number"] for i in issues],
        "states": {
            "open":   sum(1 for i in issues if i["state"] == "open"),
            "closed": sum(1 for i in issues if i["state"] == "closed"),
        }
    }

    save_json(DATA_DIR / "manifest.json", manifest)
    print(f"\n📋 Manifest saved to data/manifest.json")
    print(f"   Repo:   {manifest['repo']}")
    print(f"   Issues: {manifest['total_issues']} ({manifest['states']['open']} open, {manifest['states']['closed']} closed)")
    print(f"   Fetched at: {manifest['fetched_at']}")


# ── Main ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  Layer10 — GitHub Data Fetcher")
    print(f"  Repo: {REPO_OWNER}/{REPO_NAME}")
    print("=" * 60)

    # Step 1: Get all issues
    issues = fetch_issues()

    if not issues:
        print("❌ No issues fetched. Check your token and repo name.")
        exit(1)

    # Step 2: Get comments
    fetch_comments(issues)

    # Step 3: Get events (state changes, label changes, etc.)
    fetch_events(issues)

    # Step 4: Save manifest
    save_manifest(issues)

    print("\n🎉 Phase 1 complete! Your data is in the /data folder.")
    print("   Next step: run schema design and extraction (Phase 2 + 3)")