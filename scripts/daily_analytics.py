"""daily_analytics.py

Fault-tolerant daily ELT pipeline that extracts AhmadBilalDSA's GitHub public
events from the last 24 hours, transforms them into structured developer
metrics, appends the row to a CSV dataset, and dispatches a Discord standup
embed confirming the pipeline run.
"""

import csv
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

GITHUB_USERNAME = "AhmadBilalDSA"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
GH_TOKEN = os.getenv("GH_PAT", os.getenv("GITHUB_TOKEN", ""))
CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "developer_metrics.csv",
)
CSV_HEADERS = ["Date", "Commits", "PRs_Opened", "PRs_Reviewed", "Issues"]

EVENTS_URL = f"https://api.github.com/users/{GITHUB_USERNAME}/events/public"
EMBED_COLOR = 0x38BDF8


def _headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    return headers


def _utcnow():
    return datetime.now(timezone.utc)


def extract_events():
    """Fetch public events for the last 24 hours; returns list or empty list on failure."""
    cutoff = _utcnow() - timedelta(hours=24)
    all_events = []
    page = 1
    while page <= 5:
        try:
            resp = requests.get(
                EVENTS_URL,
                headers=_headers(),
                params={"per_page": 100, "page": page},
                timeout=30,
            )
        except requests.RequestException as exc:
            print(f"[api] events page {page} error: {exc}")
            break
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            print("[api] rate limit hit; stopping event fetch.")
            break
        if resp.status_code != 200:
            print(f"[api] events page {page} failed ({resp.status_code}): {resp.text[:200]}")
            break
        events = resp.json()
        if not events:
            break
        stop_early = False
        for event in events:
            created_at = event.get("created_at") or ""
            try:
                event_dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if event_dt < cutoff:
                stop_early = True
                break
            all_events.append(event)
        if stop_early:
            break
        page += 1
    return all_events


def transform(events):
    """Calculate daily metrics from a list of GitHub event objects."""
    commits = 0
    prs_opened = 0
    prs_reviewed = 0
    issues = 0
    for event in events:
        etype = event.get("type") or ""
        payload = event.get("payload") or {}
        if etype == "PushEvent":
            commits += len(payload.get("commits") or [])
        elif etype == "PullRequestEvent":
            action = (payload.get("action") or "").lower()
            if action == "opened":
                prs_opened += 1
        elif etype == "PullRequestReviewEvent":
            prs_reviewed += 1
        elif etype in ("IssuesEvent", "IssueCommentEvent"):
            action = (payload.get("action") or "").lower()
            if action in ("opened", "edited", "labeled", "assigned"):
                issues += 1
    return {
        "Commits": commits,
        "PRs_Opened": prs_opened,
        "PRs_Reviewed": prs_reviewed,
        "Issues": issues,
    }


def load(metrics, today_str):
    """Append today's row to the CSV dataset; create the file with headers if missing."""
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    file_exists = os.path.isfile(CSV_PATH)
    row = {
        "Date": today_str,
        "Commits": metrics["Commits"],
        "PRs_Opened": metrics["PRs_Opened"],
        "PRs_Reviewed": metrics["PRs_Reviewed"],
        "Issues": metrics["Issues"],
    }
    try:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            if not file_exists or os.path.getsize(CSV_PATH) == 0:
                writer.writeheader()
            writer.writerow(row)
        print(f"[csv] row appended: {row}")
        return True
    except OSError as exc:
        print(f"[csv] write error: {exc}")
        return False


def build_embed(metrics, today_str, csv_ok):
    """Build a Discord embed payload for the daily standup."""
    status = "CSV updated successfully" if csv_ok else "CSV update failed"
    embed = {
        "title": "\U0001F4CA Daily Developer Standup",
        "description": f"Metrics for **{today_str}** — {GITHUB_USERNAME}",
        "color": EMBED_COLOR if csv_ok else 0xEF4444,
        "fields": [
            {"name": "Commits", "value": str(metrics["Commits"]), "inline": True},
            {"name": "PRs Opened", "value": str(metrics["PRs_Opened"]), "inline": True},
            {"name": "PRs Reviewed", "value": str(metrics["PRs_Reviewed"]), "inline": True},
            {"name": "Issues Touched", "value": str(metrics["Issues"]), "inline": True},
            {"name": "Pipeline Status", "value": status, "inline": False},
        ],
        "footer": {"text": f"daily_analytics.py \u00b7 {_utcnow().strftime('%Y-%m-%d %H:%M')} UTC"},
    }
    return {"username": "Daily Analytics", "embeds": [embed]}


def dispatch_discord(payload):
    """Post the standup embed to Discord; returns True on success."""
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; embed printed to stdout instead.")
        import json
        print(json.dumps(payload, indent=2))
        return True
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=60)
    except requests.RequestException as exc:
        print(f"[discord] webhook error: {exc}")
        return False
    if resp.status_code in (200, 204):
        print("[discord] standup delivered.")
        return True
    print(f"[discord] webhook failed ({resp.status_code}): {resp.text[:300]}")
    return False


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    today_str = _utcnow().strftime("%Y-%m-%d")
    print(f"[daily-analytics] token={'configured' if GH_TOKEN else 'none'} "
          f"webhook={'configured' if DISCORD_WEBHOOK_URL else 'none'} date={today_str}")

    events = extract_events()
    print(f"[daily-analytics] fetched {len(events)} event(s) in the last 24h")

    metrics = transform(events)
    print(f"[daily-analytics] metrics: {metrics}")

    csv_ok = load(metrics, today_str)

    payload = build_embed(metrics, today_str, csv_ok)
    dispatch_discord(payload)

    return 0 if csv_ok else 1


if __name__ == "__main__":
    sys.exit(main())
