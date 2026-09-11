"""daily_analytics.py

Read-only daily ELT telemetry. Extracts AhmadBilalDSA's public GitHub events
from the last 24 hours, transforms them into structured developer metrics,
appends the row to a CSV dataset, and posts a terse internal devops audit to
Discord. The GitHub API is contacted with GET only - this script never writes,
posts, comments, or reviews on public pull requests or issues.
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
COMMIT_MESSAGE = "chore(telemetry): update daily developer metrics [skip ci]"

EVENTS_URL = f"https://api.github.com/users/{GITHUB_USERNAME}/events/public"
EMBED_COLOR = 0x38BDF8


def _utcnow():
    return datetime.now(timezone.utc)


def _gh_get(url, params=None):
    """GET-only GitHub call. Enforced read-only: no other method is ever issued."""
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    try:
        return requests.get(url, headers=headers, params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"[api] GET {url} error: {exc}")
        return None


def extract_events():
    """Fetch public events for the last 24 hours; returns list or empty list on failure."""
    cutoff = _utcnow() - timedelta(hours=24)
    all_events = []
    page = 1
    while page <= 5:
        resp = _gh_get(EVENTS_URL, params={"per_page": 100, "page": page})
        if resp is None:
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
    """Append today's row to the CSV dataset; create the file with headers if missing.

    Idempotent: if today's UTC date is already logged, the append is skipped so a
    re-run produces no diff and the workflow can skip its commit cleanly.
    """
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    if os.path.isfile(CSV_PATH):
        try:
            with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
                existing = {row["Date"] for row in csv.DictReader(f)}
            if today_str in existing:
                print(f"[csv] {today_str} already logged (idempotent); skipping append.")
                return True
        except OSError as exc:
            print(f"[csv] read error: {exc}")
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
            if os.path.getsize(CSV_PATH) == 0:
                writer.writeheader()
            writer.writerow(row)
        print(f"[csv] row appended: {row}")
        return True
    except OSError as exc:
        print(f"[csv] write error: {exc}")
        return False


def build_embed(metrics, today_str, csv_ok):
    """Build a terse internal devops audit embed; no conversational fluff."""
    status = "appended" if csv_ok else "write failed"
    prs_touched = metrics["PRs_Opened"] + metrics["PRs_Reviewed"]
    embed = {
        "title": "Engineering Activity Snapshot",
        "color": EMBED_COLOR if csv_ok else 0xEF4444,
        "fields": [
            {"name": "Date/UTC", "value": today_str, "inline": True},
            {"name": "Commits (Logged)", "value": str(metrics["Commits"]), "inline": True},
            {"name": "PRs Touched", "value": str(prs_touched), "inline": True},
            {"name": "Issues Touched", "value": str(metrics["Issues"]), "inline": True},
            {"name": "Dataset", "value": f"data/developer_metrics.csv: {status}", "inline": False},
        ],
        "footer": {"text": f"daily_analytics.py \u00b7 {_utcnow().strftime('%Y-%m-%d %H:%M')} UTC"},
    }
    return {"username": "Telemetry", "embeds": [embed]}


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
