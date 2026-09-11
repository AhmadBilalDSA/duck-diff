"""daily_analytics.py

Ultra-light dual-run telemetry runner. Triggered twice a day by
.github/workflows/daily_analytics.yml:

  * Morning phase (06:00 UTC): snapshot upstream PR + CI health into
    data/portfolio_status.json.
  * Evening phase (18:00 UTC): append today's engineering summary to
    data/developer_metrics.csv.

Pure standard library only (urllib / json / csv / subprocess) so the script
starts instantly with zero dependency-install lag. Every file read/write is
confined to the repository's data/ directory; duck_diff/, tests, and config
files are never touched.

Skip guard: if the metrics are unchanged, or the current phase is already
recorded for today, the script exits 0 immediately without any git commit or
push. When a change is real, only the targeted data/ file is staged and
committed with a phase-specific [skip ci] message before pushing.
"""

import csv
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

GITHUB_USERNAME = os.getenv("GH_USERNAME", "") or "AhmadBilalDSA"
REPO = os.getenv("GITHUB_REPOSITORY", "") or f"{GITHUB_USERNAME}/duck-diff"
GH_TOKEN = os.getenv("GH_PAT", os.getenv("GITHUB_TOKEN", ""))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PORTFOLIO_PATH = os.path.join(DATA_DIR, "portfolio_status.json")
CSV_PATH = os.path.join(DATA_DIR, "developer_metrics.csv")

CSV_HEADERS = ["Date", "Commits", "PRs_Opened", "PRs_Reviewed", "Issues"]
MORNING_COMMIT = "data(portfolio): snapshot upstream pr & ci health [skip ci]"
EVENING_COMMIT = "chore(telemetry): append daily engineering metrics [skip ci]"

GIT_USER = "github-actions[bot]"
GIT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

EVENTS_URL = f"https://api.github.com/users/{GITHUB_USERNAME}/events/public"
REPO_URL = f"https://api.github.com/repos/{REPO}"
RUNS_URL = f"https://api.github.com/repos/{REPO}/actions/runs"


def _utcnow():
    return datetime.now(timezone.utc)


def _gh_get(url, params=None):
    """GET-only GitHub API call. Returns (data, link) or (None, None) on failure."""
    headers = {"User-Agent": "duck-diff-daily-analytics", "Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = url + ("&" if "?" in url else "?") + query
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            link = resp.headers.get("Link")
            if not body:
                return None, link
            return json.loads(body), link
    except urllib.error.HTTPError as exc:
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0":
            print("[api] rate limit hit; aborting API fetches.")
        else:
            print(f"[api] GET {url} failed ({exc.code})")
        return None, None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[api] GET {url} error: {exc}")
        return None, None


def extract_events():
    """Fetch public events for the last 24 hours; returns list or empty list on failure."""
    cutoff = _utcnow() - timedelta(hours=24)
    all_events = []
    page = 1
    while page <= 5:
        resp, _ = _gh_get(EVENTS_URL, {"per_page": 100, "page": page})
        if resp is None:
            break
        if not isinstance(resp, list) or not resp:
            break
        stop_early = False
        for event in resp:
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


def _stable_snapshot(payload):
    """Return snapshot fields excluding the transient 'as_of' timestamp."""
    return {k: v for k, v in payload.items() if k != "as_of"}


def _fetch_ci_health():
    runs_data, _ = _gh_get(RUNS_URL, {"per_page": 5})
    if not isinstance(runs_data, dict):
        return {"runs": None, "last_conclusion": None, "last_run_created": None}
    runs = runs_data.get("workflow_runs") or []
    first = runs[0] if runs else {}
    return {
        "runs": len(runs),
        "last_conclusion": first.get("conclusion"),
        "last_run_created": first.get("created_at"),
    }


def snapshot_portfolio():
    """Morning phase: snapshot PR + CI health into data/portfolio_status.json."""
    now = _utcnow()
    date_str = now.strftime("%Y-%m-%d")
    existing = {}
    if os.path.isfile(PORTFOLIO_PATH):
        try:
            with open(PORTFOLIO_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"[portfolio] read error: {exc}")

    if isinstance(existing, dict) and existing.get("date") == date_str:
        print(f"[portfolio] {date_str} already snapshotted (idempotent); skipping early.")
        return 0, False

    payload = {
        "date": date_str,
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repository": REPO,
    }
    repo_data, _ = _gh_get(REPO_URL)
    if isinstance(repo_data, dict):
        payload["open_pull_requests"] = repo_data.get("open_pulls_count")
        payload["open_issues"] = repo_data.get("open_issues_count")
    payload["ci_health"] = _fetch_ci_health()

    if isinstance(existing, dict) and _stable_snapshot(existing) == _stable_snapshot(payload):
        print("[portfolio] no metrics changed; skipping.")
        return 0, False

    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"[portfolio] snapshot written: {PORTFOLIO_PATH}")
        return 0, True
    except OSError as exc:
        print(f"[portfolio] write error: {exc}")
        return 1, False


def append_metrics(events):
    """Evening phase: append today's summary row to data/developer_metrics.csv."""
    date_str = _utcnow().strftime("%Y-%m-%d")
    existing = []
    if os.path.isfile(CSV_PATH):
        try:
            with open(CSV_PATH, "r", newline="", encoding="utf-8") as f:
                existing = [row.get("Date") or "" for row in csv.DictReader(f)]
        except OSError as exc:
            print(f"[csv] read error: {exc}")
    if date_str in existing:
        print(f"[csv] {date_str} already logged (idempotent); skipping early.")
        return 0, False

    metrics = transform(events)
    row = {
        "Date": date_str,
        "Commits": metrics["Commits"],
        "PRs_Opened": metrics["PRs_Opened"],
        "PRs_Reviewed": metrics["PRs_Reviewed"],
        "Issues": metrics["Issues"],
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        fresh_file = not os.path.isfile(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            if fresh_file:
                writer.writeheader()
            writer.writerow(row)
        print(f"[csv] row appended: {row}")
        return 0, True
    except OSError as exc:
        print(f"[csv] write error: {exc}")
        return 1, False


def _git(args):
    """Run a git command from the repo root; returns returncode or None on error."""
    try:
        proc = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[git] error running git {' '.join(args)}: {exc}")
        return None
    if proc.returncode != 0:
        print(f"[git] git {' '.join(args)} -> {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.returncode


def commit_and_push(file_rel, message):
    """Stage only the targeted data/ file, commit with [skip ci], then push.

    Returns True when the call completed (including clean skip or local dry-run);
    False when a hard failure occurred (git unavailable or commit rejected).
    """
    if os.environ.get("CI") != "true":
        print(f"[git] local run detected; not committing (would stage {file_rel} with '{message}').")
        return True
    if _git(["diff", "--quiet"]) not in (0, None):
        print("[git] working tree is dirty; refusing to autocommit.")
        return False
    if _git(["config", "user.name", GIT_USER]) != 0:
        return False
    if _git(["config", "user.email", GIT_EMAIL]) != 0:
        return False
    if _git(["add", "--", file_rel]) != 0:
        return False
    if _git(["diff", "--cached", "--quiet"]) == 0:
        print(f"[git] {file_rel} has no staged changes; skipping commit.")
        return True
    if _git(["commit", "-m", message]) != 0:
        return False
    if _git(["push", "origin", "HEAD"]) != 0:
        print("[git] push failed; commit remains local.")
        return False
    print(f"[git] committed and pushed: {message}")
    return True


def run_morning():
    date_str = _utcnow().strftime("%Y-%m-%d")
    print(f"[daily-analytics] morning phase date={date_str} token={'configured' if GH_TOKEN else 'none'}")
    status, changed = snapshot_portfolio()
    if not changed:
        return status
    return 0 if commit_and_push("data/portfolio_status.json", MORNING_COMMIT) else 1


def run_evening():
    date_str = _utcnow().strftime("%Y-%m-%d")
    print(f"[daily-analytics] evening phase date={date_str} token={'configured' if GH_TOKEN else 'none'}")
    events = extract_events()
    print(f"[daily-analytics] fetched {len(events)} event(s) in the last 24h")
    status, changed = append_metrics(events)
    if not changed:
        return status
    return 0 if commit_and_push("data/developer_metrics.csv", EVENING_COMMIT) else 1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if _utcnow().hour < 12:
        return run_morning()
    return run_evening()


if __name__ == "__main__":
    sys.exit(main())