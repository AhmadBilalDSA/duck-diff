"""oss_tracker.py

Fault-tolerant open source activity tracker for Ahmad Bilal's portfolio.

Sweeps the GitHub search and REST APIs for pull requests, issue updates, and
review requests authored by or assigned to AhmadBilalDSA (excluding beginner
repos), assesses review/CI/merge state, and posts a clean status card to a
Discord webhook. Every API call is guarded by try/except and falls back to
partial reports when rate limits or transient errors hit - the scheduled
workflow never crashes on API trouble.
"""

import json
import os
import sys
import requests
from datetime import datetime, timedelta, timezone

GITHUB_USERNAME = "AhmadBilalDSA"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# A dedicated PAT (GH_PAT) beats the Actions-default GITHUB_TOKEN when present.
GH_TOKEN = os.getenv("GH_PAT", os.getenv("GITHUB_TOKEN", ""))

# Explicit multi-repo portfolio the tracker must cover.
PORTFOLIO_REPOS = [
    "langchain-ai/langchain",
    "sqlfluff/sqlfluff",
    "ibis-project/ibis",
    "sara-czasak/py-simple-wrap",
    "pingcap/tidb",
    "semantica-agi/semantica",
    "AhmadBilalDSA/duck-diff",
]

TITLE_OWNER = {
    "langchain-ai": "langchain",
    "sqlfluff": "sqlfluff",
    "ibis-project": "ibis",
    "sara-czasak": "py-simple-wrap",
    "pingcap": "tidb",
    "semantica-agi": "semantica",
    "AhmadBilalDSA": "duck-diff",
}

EXCLUDE_SUBSTRING = "first-contribution"
MERGE_WINDOW_DAYS = 7
ISSUE_WINDOW_DAYS = 14
MAX_OPEN_AUDS = 12
MAX_LIST_ITEMS = 8
FIELD_CHAR_LIMIT = 1024
SEARCH_URL = "https://api.github.com/search/issues"
API_BASE = "https://api.github.com"

# Discord embed colors
EMERALD = 0x34D399
CYAN = 0x38BDF8
AMBER = 0xF59E0B
RED = 0xEF4444

FAILING_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled"}


class _GitHubRateLimit(Exception):
    """Raised when the GitHub API returns 403 with X-RateLimit-Remaining: 0."""


class _SearchFailure(Exception):
    """Raised when a GitHub search call fails for a non-rate-limit reason."""


def _headers():
    headers = {"Accept": "application/vnd.github+json"}
    if GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    return headers


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _short_date(dt):
    return dt.strftime("%b %d") if dt else ""


def _gh_get(url, params=None):
    """GET a GitHub endpoint; returns parsed JSON or raises/None on trouble."""
    try:
        resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"[api] GET {url} error: {exc}")
        return None
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        raise _GitHubRateLimit(url)
    if resp.status_code != 200:
        print(f"[api] GET {url} failed ({resp.status_code}): {resp.text[:200]}")
        return None
    return resp.json()


def _search_issues(query, per_page=30):
    # Build the URL manually: requests would percent-encode `+` and `:` in the
    # query dict, which GitHub's search parser rejects (422). GitHub expects
    # the raw `+` separators bracketing the `<qualifier>:<value>` tokens.
    url = f"{SEARCH_URL}?q={query}&sort=updated&order=desc&per_page={per_page}"
    data = _gh_get(url)
    if data is None:
        return None
    return data.get("items", []) or []


def _repo_of(item):
    return (item.get("repository_url") or "").replace("https://api.github.com/repos/", "")


def _repo_label(repo):
    owner, _, name = repo.partition("/")
    return TITLE_OWNER.get(owner, name or repo)


def _is_excluded(repo):
    return not repo or EXCLUDE_SUBSTRING in repo.lower()


def _item_title(item):
    return " ".join((item.get("title") or "Untitled").split())


# ---------------------------------------------------------------------------
# Data collection (each stage degrades to a note on API trouble)
# ---------------------------------------------------------------------------
def _fetch_open_prs():
    items = _search_issues(f"author:{GITHUB_USERNAME}+is:pr+is:open")
    if items is None:
        raise _SearchFailure("open PR search failed")
    out = []
    for item in items:
        repo = _repo_of(item)
        if _is_excluded(repo):
            continue
        out.append({
            "repo": repo,
            "number": item.get("number", 0),
            "title": _item_title(item),
            "html_url": item.get("html_url") or "",
            "updated_at": item.get("updated_at") or "",
            "is_portfolio": repo in PORTFOLIO_REPOS,
            "ci": None,
            "flags": [],
            "pending_review": False,
            "audit_failed": False,
        })
    return out


def _ci_state(repo, sha):
    data = _gh_get(f"{API_BASE}/repos/{repo}/commits/{sha}/check-runs")
    if data is None:
        combined = _gh_get(f"{API_BASE}/repos/{repo}/commits/{sha}/status")
        return (combined or {}).get("state", "unknown")
    runs = data.get("check_runs") or []
    if not runs:
        return "no-checks"
    if any(r.get("conclusion") in FAILING_CONCLUSIONS for r in runs):
        return "failure"
    if any(r.get("status") != "completed" for r in runs):
        return "pending"
    return "success"


def _audit_open_pr(entry):
    repo, number = entry["repo"], entry["number"]
    try:
        detail = _gh_get(f"{API_BASE}/repos/{repo}/pulls/{number}")
        if detail is None:
            entry["audit_failed"] = True
            return entry
        if detail.get("mergeable") is False or detail.get("mergeable_state") == "dirty":
            entry["flags"].append("merge conflict")
        sha = (detail.get("head") or {}).get("sha")
        if sha:
            state = _ci_state(repo, sha)
            entry["ci"] = state
            if state == "failure":
                entry["flags"].append("CI failing")
            elif state == "pending":
                entry["flags"].append("CI pending")
        comments = _gh_get(f"{API_BASE}/repos/{repo}/pulls/{number}/comments")
        if comments:
            last_author = (comments[-1].get("user") or {}).get("login")
            if last_author and last_author.lower() != GITHUB_USERNAME.lower():
                entry["pending_review"] = True
                entry["flags"].append("maintainer comment awaiting reply")
    except _GitHubRateLimit:
        entry["audit_failed"] = True
    except Exception as exc:
        entry["audit_failed"] = True
        print(f"[audit] repo={repo} pr={number} error: {exc}")
    return entry


def _fetch_merged_prs():
    cutoff = _utcnow() - timedelta(days=MERGE_WINDOW_DAYS)
    items = _search_issues(f"author:{GITHUB_USERNAME}+is:pr+is:merged")
    if items is None:
        raise _SearchFailure("merged PR search failed")
    out = []
    for item in items:
        repo = _repo_of(item)
        if _is_excluded(repo):
            continue
        merged_at = (item.get("pull_request") or {}).get("merged_at") or (item.get("closed_at") or "")
        merged_dt = _parse_dt(merged_at)
        if merged_dt is None or merged_dt < cutoff:
            continue
        out.append({
            "repo": repo,
            "number": item.get("number", 0),
            "title": _item_title(item),
            "html_url": item.get("html_url") or "",
            "merged_at": merged_dt,
            "is_portfolio": repo in PORTFOLIO_REPOS,
        })
    return out


def _fetch_review_requests():
    items = _search_issues(f"review-requested:{GITHUB_USERNAME}+is:pr+is:open", per_page=20)
    if items is None:
        raise _SearchFailure("review request search failed")
    out = []
    for item in items:
        repo = _repo_of(item)
        if _is_excluded(repo):
            continue
        out.append({
            "repo": repo,
            "number": item.get("number", 0),
            "title": _item_title(item),
            "html_url": item.get("html_url") or "",
            "is_portfolio": repo in PORTFOLIO_REPOS,
        })
    return out


def _fetch_open_issues():
    cutoff = _utcnow() - timedelta(days=ISSUE_WINDOW_DAYS)
    items = _search_issues(f"author:{GITHUB_USERNAME}+is:issue+is:open")
    if items is None:
        raise _SearchFailure("issue search failed")
    out = []
    for item in items:
        repo = _repo_of(item)
        if _is_excluded(repo):
            continue
        updated = _parse_dt(item.get("updated_at") or "")
        if updated is None or updated < cutoff:
            continue
        out.append({
            "repo": repo,
            "number": item.get("number", 0),
            "title": _item_title(item),
            "html_url": item.get("html_url") or "",
            "updated_at": updated,
            "is_portfolio": repo in PORTFOLIO_REPOS,
        })
    return out


def collect_activity():
    """Collect all GitHub activity; each stage is rate-limit and error tolerant."""
    data = {"open_prs": [], "merged_prs": [], "review_requests": [], "open_issues": [], "notes": []}

    def _guarded(label, fn, key):
        try:
            data[key] = fn()
        except _GitHubRateLimit:
            data["notes"].append(f"Rate limit reached fetching {label}; showing partial data.")
        except _SearchFailure:
            data["notes"].append(f"GitHub search unavailable for {label}; showing partial data.")

    _guarded("open PRs", _fetch_open_prs, "open_prs")
    _guarded("merged PRs", _fetch_merged_prs, "merged_prs")
    _guarded("review requests", _fetch_review_requests, "review_requests")
    _guarded("issues", _fetch_open_issues, "open_issues")

    for entry in data["open_prs"][:MAX_OPEN_AUDS]:
        _audit_open_pr(entry)

    undetailed = [e for e in data["open_prs"] if e.get("audit_failed")]
    if undetailed:
        data["notes"].append(
            f"{len(undetailed)} open PR(s) not fully assessed (rate limit / API unavailable); "
            "merge-conflict and CI state may be missing for those."
        )

    return data


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _entry(prefix, item, suffix=""):
    title = (item.get("title") or "Untitled")[:110]
    line = f"**{prefix}** {title}"
    if suffix:
        line += f" ({suffix})"
    url = item.get("html_url") or ""
    return f"{line}\n{url}"


def _clip(lines):
    if len(lines) <= MAX_LIST_ITEMS:
        return lines
    extra = len(lines) - MAX_LIST_ITEMS
    return lines[:MAX_LIST_ITEMS] + [f"*...and {extra} more*"]


def _field_value(lines, fallback):
    if not lines:
        return fallback
    text = "\n".join(_clip(lines))
    if len(text) <= FIELD_CHAR_LIMIT:
        return text
    kept, total = [], 0
    for ln in lines:
        if total + len(ln) + 1 > FIELD_CHAR_LIMIT - 24:
            break
        kept.append(ln)
        total += len(ln) + 1
    text = "\n".join(kept)
    extra = len(lines) - len(kept)
    if extra:
        text += f"\n*...and {extra} more*"
    return text[:FIELD_CHAR_LIMIT]


def build_report(data):
    active = []
    for rr in data["review_requests"]:
        active.append(_entry(f"[{_repo_label(rr['repo'])}] PR#{rr['number']} (review requested)", rr))
    for pr in data["open_prs"]:
        if "maintainer comment awaiting reply" in pr["flags"]:
            active.append(_entry(f"[{_repo_label(pr['repo'])}] PR#{pr['number']}", pr, "reply pending"))

    merged = [
        _entry(f"[{_repo_label(m['repo'])}] PR#{m['number']}", m, _short_date(m["merged_at"]))
        for m in data["merged_prs"]
    ]

    open_items = []
    for pr in data["open_prs"]:
        for flag in pr["flags"]:
            if flag == "maintainer comment awaiting reply":
                continue
            open_items.append(_entry(f"[{_repo_label(pr['repo'])}] PR#{pr['number']}", pr, flag))
    for issue in data["open_issues"]:
        open_items.append(
            _entry(f"[{_repo_label(issue['repo'])}] Issue#{issue['number']}", issue, f"updated {_short_date(issue['updated_at'])}")
        )

    portfolio_hit = {m["repo"] for m in data["merged_prs"]} | {p["repo"] for p in data["open_prs"]}
    portfolio_seen = [r for r in PORTFOLIO_REPOS if r in portfolio_hit]

    return {
        "active": active,
        "merged": merged,
        "open_items": open_items,
        "notes": data["notes"],
        "counts": {
            "open": len(data["open_prs"]),
            "merged": len(data["merged_prs"]),
            "flags": len(open_items),
            "reviews": len(data["review_requests"]),
            "issues": len(data["open_issues"]),
            "portfolio": portfolio_seen,
        },
    }


def build_payload(report):
    counts = report["counts"]
    snapshot = (
        f"**Snapshot:** {counts['open']} open PRs \u00b7 {counts['merged']} merged (7d) \u00b7 "
        f"{counts['flags']} flags \u00b7 {counts['reviews']} review requests \u00b7 "
        f"{counts['issues']} issues touched"
    )
    portfolio = ", ".join(_repo_label(r) for r in counts["portfolio"]) or "none this cycle"

    if report["notes"] or counts["flags"]:
        color = AMBER
    elif counts["open"] or counts["reviews"]:
        color = CYAN
    else:
        color = EMERALD

    fields = [
        {"name": "Active Reviews", "value": _field_value(report["active"], "No pending review discussions."), "inline": False},
        {"name": "Merged PRs (7d)", "value": _field_value(report["merged"], "No recent merges in the window."), "inline": False},
        {"name": "Open Items", "value": _field_value(report["open_items"], "All checks green, no conflicts, no stale issues."), "inline": False},
    ]
    if report["notes"]:
        fields.append({"name": "Tracker Notes", "value": _field_value([f"- {n}" for n in report["notes"]], "-"), "inline": False})

    embed = {
        "title": "Open-Source Activity Snapshot",
        "description": f"{snapshot}\n\nPortfolio active: {portfolio}",
        "color": color,
        "footer": {"text": f"duck-diff \u00b7 OSS Activity Tracker \u00b7 {_utcnow().strftime('%Y-%m-%d %H:%M')} UTC"},
    }
    return {"username": "OSS Activity Tracker", "embeds": [embed]}


# ---------------------------------------------------------------------------
# Console report (mirrors the Discord card for CI logs)
# ---------------------------------------------------------------------------
def print_report(report):
    sep = "=" * 60
    print("\n" + sep)
    print("OSS ACTIVITY SNAPSHOT")
    print(sep)

    def dump(label, lines):
        print(f"\n[{label}]")
        if not lines:
            print("  (none)")
        for ln in lines:
            print(f"  - {ln.splitlines()[0]}")

    dump("Active Reviews", report["active"])
    dump("Merged PRs (7d)", report["merged"])
    dump("Open Items", report["open_items"])
    if report["notes"]:
        dump("Tracker Notes", report["notes"])

    c = report["counts"]
    print(f"\nCounts: open={c['open']} merged={c['merged']} flags={c['flags']} "
          f"reviews={c['reviews']} issues={c['issues']} portfolio={c['portfolio'] or 'none'}")
    print(sep)


# ---------------------------------------------------------------------------
# Discord dispatch
# ---------------------------------------------------------------------------
def dispatch(payload):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; status card printed to stdout instead.")
        return True
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=60)
    except requests.RequestException as exc:
        print(f"[discord] webhook error: {exc}")
        return False
    if resp.status_code in (200, 204):
        print("[discord] status card delivered to Discord.")
        return True
    print(f"[discord] webhook failed ({resp.status_code}): {resp.text[:300]}")
    return False


def dispatch_error(message):
    payload = {
        "username": "OSS Activity Tracker",
        "embeds": [{
            "title": "OSS Tracker Error",
            "description": message,
            "color": RED,
        }],
    }
    print(f"[fatal] {message}")
    return dispatch(payload)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    print(f"[oss-tracker] token={'configured' if GH_TOKEN else 'none'} "
          f"webhook={'configured' if DISCORD_WEBHOOK_URL else 'none'}")

    try:
        data = collect_activity()
    except Exception as exc:
        print(f"[oss-tracker] collector crashed: {exc}")
        ok = dispatch_error(f"oss_tracker crashed: {exc}")
        return 0 if ok else 1

    report = build_report(data)
    print_report(report)

    payload = build_payload(report)
    print("\n[discord] payload preview:")
    print(json.dumps(payload, indent=2))
    ok = dispatch(payload)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())