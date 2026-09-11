import os
import re
import sys
import requests
import json
from datetime import datetime, timedelta, timezone

GITHUB_USERNAME = "AhmadBilalDSA"
REPO_NAME = "duck-diff"

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
GH_PAT = os.getenv("GH_PAT", os.getenv("GITHUB_TOKEN", ""))
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
# AUTO_PUBLISH defaults to 'true' unless explicitly set to 'false'
_auto_publish_raw = os.getenv("AUTO_PUBLISH", "").strip().lower()
AUTO_PUBLISH = "false" if _auto_publish_raw == "false" else "true"

# Content audit guardrails & SEO footer
BANNED_CLICHES = ["thrilled", "excited to share", "humbled", "delighted"]
REQUIRED_HASHTAGS = ["#DataEngineering", "#DuckDB", "#AnalyticsEngineering", "#Python"]
HASHTAGS_LINE = " ".join(REQUIRED_HASHTAGS)
MIN_BODY_CHARS, MAX_BODY_CHARS = 500, 2000
RECENT_PR_DAYS = 14

# Sanitize LinkedIn credentials: strip extra quotes, surrounding quotes, \r/\n and spaces
def sanitize_secret(value):
    value = (value or "").replace("\r", "").replace("\n", "")
    prev = None
    while prev != value:
        prev = value
        value = value.strip().strip("\"'`").strip()
    return value


LINKEDIN_ACCESS_TOKEN = sanitize_secret(os.getenv("LINKEDIN_ACCESS_TOKEN", ""))
LINKEDIN_PERSON_URN = sanitize_secret(os.getenv("LINKEDIN_PERSON_URN", ""))
if LINKEDIN_PERSON_URN and not LINKEDIN_PERSON_URN.startswith("urn:li:person:"):
    LINKEDIN_PERSON_URN = f"urn:li:person:{LINKEDIN_PERSON_URN}"

LINKEDIN_HEADERS = {
    "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
    "Content-Type": "application/json",
    "LinkedIn-Version": "202601",
    "X-Restli-Protocol-Version": "2.0.0",
}
BANNER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")


# ---------------------------------------------------------------------------
# Step 1: GitHub PR discovery (14-day window)
# ---------------------------------------------------------------------------
def fetch_recent_merged_pr():
    url = f"https://api.github.com/search/issues?q=author:{GITHUB_USERNAME}+is:pr+is:merged&per_page=20&sort=updated&order=desc"
    headers = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github+json"} if GH_PAT else {}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"[github] search failed ({resp.status_code}): {resp.text}")
        return None
    items = resp.json().get("items", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_PR_DAYS)
    for item in items:
        closed_at = item.get("closed_at")
        if not closed_at:
            continue
        try:
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if closed_dt < cutoff:
            continue
        repo_full = (item.get("repository_url") or "").replace("https://api.github.com/repos/", "") or REPO_NAME
        return {
            "title": item["title"],
            "html_url": item["html_url"],
            "body": (item.get("body") or "")[:800],
            "repository_url": repo_full,
            "number": item.get("number", 0),
        }
    return None


def get_recent_pr():
    try:
        return fetch_recent_merged_pr()
    except Exception as e:
        print(f"[github] error querying API: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 1b: Topic rotation engine (deterministic by UTC weekday)
# ---------------------------------------------------------------------------
TOPICS_BY_WEEKDAY = {
    0: ("Processing & DuckDB performance",
        "DuckDB in-memory aggregation vs spill-to-disk, vectorized execution throughput, and TPC-H scale performance."),
    1: ("Defensive data assertions & validation",
        "Row-count equality checks, schema contracts, and automated guardrails that fail fast in CI."),
    2: ("Data modeling: star schema vs OBT",
        "Join fan-out, query latency, and storage trade-offs between normalized star models and one-big-table."),
    3: ("Upstream open-source contribution patterns",
        "Contributor onboarding, merge hygiene, and sustainable OSS maintenance workflows."),
}


def get_topic_context(pr):
    if pr:
        print(f"[topic] recent merged PR prioritized (last {RECENT_PR_DAYS} days).")
        return {
            "mode": "PR",
            "title": pr["title"],
            "body": pr.get("body") or "No PR description provided.",
            "repository_url": pr["repository_url"],
            "html_url": pr["html_url"],
            "number": pr.get("number", 0),
        }
    weekday = datetime.now(timezone.utc).weekday()
    title, brief = TOPICS_BY_WEEKDAY.get(weekday, TOPICS_BY_WEEKDAY[weekday % 4])
    print(f"[topic] no recent PR; rotating topic (UTC weekday {weekday}): {title}")
    return {
        "mode": "TOPIC",
        "title": title,
        "body": brief,
        "repository_url": REPO_NAME,
        "html_url": f"https://github.com/{GITHUB_USERNAME}/{REPO_NAME}",
        "number": None,
    }


# ---------------------------------------------------------------------------
# Step 2: OpenRouter free model discovery
# ---------------------------------------------------------------------------
def discover_free_models():
    try:
        resp = requests.get(f"{AI_BASE_URL}/models", timeout=30)
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            free = [
                m["id"] for m in models
                if str(m.get("id", "")).endswith(":free")
                or (m.get("pricing", {}).get("prompt") == "0"
                    and m.get("pricing", {}).get("completion") == "0")
            ]
            print(f"[openrouter] discovered {len(free)} active free models")
            return free
        print(f"[openrouter] model catalog failed ({resp.status_code})")
    except Exception as e:
        print(f"[openrouter] error: {e}")
    return ["deepseek/deepseek-chat:free", "google/gemini-flash-1.5:free"]


# ---------------------------------------------------------------------------
# Step 3: AI post drafting
# ---------------------------------------------------------------------------
def generate_post(ctx):
    if not AI_API_KEY:
        print("ERROR: AI_API_KEY is missing or empty.")
        return None

    system_prompt = (
        "You are a senior Analytics Engineer writing high-conversion LinkedIn posts "
        "for a technical audience. Follow these rules exactly:\n"
        "1. Hook: the FIRST line must open with a concrete numeric metric "
        "(latency ms, memory footprint MB, row count, throughput).\n"
        "2. Write exactly 3 technical density points covering memory footprints, "
        "vectorized execution, and schema trade-offs.\n"
        "3. Thread natural semantic keywords throughout for search discovery "
        "(DuckDB, data engineering, analytics engineering, columnar, parquet, "
        "validation, CI/CD).\n"
        "4. End with an open systems question to drive comment engagement.\n"
        "5. First-person voice, zero corporate cliches (thrilled, excited to share, "
        "humbled, delighted), no emojis.\n"
        "Return ONLY the post body text. Do NOT add hashtags or a URL line - "
        "those are appended automatically. Do not use section markers."
    )
    user_prompt = (
        f"Draft ONE LinkedIn post on this topic.\n"
        f"Topic: {ctx['title']}\nTechnical brief: {ctx['body']}\n"
        f"Repository: {ctx['repository_url']}\nReference URL: {ctx['html_url']}\n"
        f"Target 700-1200 characters for substance and LinkedIn algorithm favor."
    )
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": f"https://github.com/{GITHUB_USERNAME}/{REPO_NAME}",
        "X-Title": f"{REPO_NAME} PR-to-LinkedIn Engine",
    }

    for model in discover_free_models()[:5]:
        print(f"[ai] attempting draft with {model} ...")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.4,
        }
        try:
            resp = requests.post(f"{AI_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=90)
            if resp.status_code == 200:
                text = resp.json()["choices"][0]["message"]["content"]
                return parse_post(text)
            print(f"[ai] model {model} failed ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            print(f"[ai] model {model} error: {e}")
    return None


def parse_post(text):
    text = re.sub(r"\[POST BODY\]\s*", "", text or "", flags=re.I)
    text = re.sub(r"\[FIRST COMMENT\].*", "", text, flags=re.S | re.I)
    return text.strip()


def append_footer(body, ctx):
    body = (body or "").strip()
    reference_line = f"PR: {ctx['html_url']}" if ctx["mode"] == "PR" else f"Code: {ctx['html_url']}"
    if reference_line not in body:
        body += f"\n\n{reference_line}"
    if HASHTAGS_LINE not in body:
        body += f"\n{HASHTAGS_LINE}"
    return body


# ---------------------------------------------------------------------------
# Step 3b: Automated safety & SEO audit engine
# ---------------------------------------------------------------------------
def run_automated_audit(body, reference_url):
    reasons = []
    body = body or ""
    body_len = len(body)
    if not (MIN_BODY_CHARS <= body_len <= MAX_BODY_CHARS):
        reasons.append(
            f"Body length {body_len} chars is outside the {MIN_BODY_CHARS}-{MAX_BODY_CHARS} character range."
        )
    lower_body = body.lower()
    cliches_found = [c for c in BANNED_CLICHES if c in lower_body]
    if cliches_found:
        reasons.append(
            f"Banned corporate cliches detected: {', '.join(cliches_found)}."
        )
    hook = body.strip().splitlines()[0].strip() if body.strip() else ""
    if not re.search(r"\d+", hook):
        reasons.append(
            "Hook (first sentence) contains no concrete number or metric (regex r'\\d+')."
        )
    missing_hashtags = [h for h in REQUIRED_HASHTAGS if h not in body]
    if missing_hashtags:
        reasons.append(f"Missing required hashtags: {', '.join(missing_hashtags)}.")
    if reference_url and reference_url not in body:
        reasons.append(f"Missing inline reference URL: {reference_url}.")
    passed = len(reasons) == 0
    if passed:
        print(f"[audit] PASS - body {body_len} chars, numeric hook, cliches 0, SEO footer OK.")
    else:
        print(f"[audit] FAIL - {len(reasons)} issue(s):")
        for reason in reasons:
            print(f"  - {reason}")
    return passed, reasons


# ---------------------------------------------------------------------------
# Step 4: Banner generation with matplotlib
# ---------------------------------------------------------------------------
def generate_banner(ctx):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=120)
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.75)
    ax.axis("off")

    title = ctx["title"]
    if len(title) > 70:
        title = title[:67].rsplit(" ", 1)[0] + "..."

    if ctx.get("number"):
        badge = f"PR #{ctx['number']}  //  {ctx['repository_url']}"
    else:
        badge = f"{ctx['repository_url']}  //  open-source engineering"

    ax.text(0.6, 4.7, badge, color="#94a3b8", fontsize=13, va="center", family="monospace")
    ax.text(0.6, 3.4, title, color="#f8fafc", fontsize=26, va="center",
            family="sans-serif", fontweight="bold", wrap=True)
    ax.text(0.6, 2.0, "Automated technical storytelling  •  powered by GitHub Actions + duck-diff",
            color="#38bdf8", fontsize=13, va="center", family="monospace")
    ax.plot([0.6, 11.4], [1.35, 1.35], color="#334155", lw=2)
    ax.text(0.6, 0.6, "github.com/" + GITHUB_USERNAME + "/" + REPO_NAME,
            color="#64748b", fontsize=12, va="center", family="monospace")

    fig.savefig(BANNER_PATH, facecolor="#0f172a", bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    print(f"[banner] generated {BANNER_PATH}")
    return BANNER_PATH


# ---------------------------------------------------------------------------
# Step 5: Discord dispatch (with banner attachment)
# ---------------------------------------------------------------------------
def dispatch_discord(body, banner_path):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; skipping dispatch.")
        return
    print("[discord] dispatching post + banner to webhook...")
    try:
        message = (
            f"**New technical post ready for {REPO_NAME}:**\n\n"
            f"{body}\n\nCharacter count: {len(body)} / {MAX_BODY_CHARS}"
        )
        with open(banner_path, "rb") as f:
            files = {"file": ("banner.png", f, "image/png")}
            payload = {"content": message}
            resp = requests.post(DISCORD_WEBHOOK_URL, data=payload, files=files, timeout=60)
        if resp.status_code in (200, 204):
            print("[discord] delivered to channel successfully.")
        else:
            print(f"[discord] webhook failed ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"[discord] error: {e}")


def dispatch_discord_warning(reasons, body):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; audit failure not dispatched.")
        return
    print("[discord] dispatching audit-failure warning to webhook...")
    try:
        message = (
            f"**AUDIT FAILED - post NOT published to LinkedIn.**\n\n"
            f"Failure reasons:\n" + "\n".join(f"- {r}" for r in reasons) +
            f"\n\n**Draft:**\n{body[:1500]}\n\nCharacter count: {len(body)}"
        )
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=60)
        if resp.status_code in (200, 204):
            print("[discord] audit-failure warning delivered.")
        else:
            print(f"[discord] warning webhook failed ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"[discord] error sending warning: {e}")


def dispatch_discord_success(post_urn, banner_path, body):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; success not dispatched.")
        return
    print("[discord] dispatching publish confirmation to webhook...")
    try:
        message = (
            f"**Publish confirmed - live on LinkedIn.**\n\n"
            f"Post URN: `{post_urn}`\n"
            f"Live URL: https://www.linkedin.com/feed/update/{post_urn}\n"
            f"Character count: {len(body)} / {MAX_BODY_CHARS}\n\n"
            f"Banner preview attached."
        )
        with open(banner_path, "rb") as f:
            files = {"file": ("banner.png", f, "image/png")}
            resp = requests.post(DISCORD_WEBHOOK_URL, data={"content": message}, files=files, timeout=60)
        if resp.status_code in (200, 204):
            print("[discord] success confirmation delivered.")
        else:
            print(f"[discord] success webhook failed ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"[discord] error sending success: {e}")


# ---------------------------------------------------------------------------
# Step 6: LinkedIn publishing
# ---------------------------------------------------------------------------
def validate_linkedin():
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_PERSON_URN:
        print("[linkedin] LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN missing; skipping publish.")
        return False
    if not LINKEDIN_PERSON_URN.startswith("urn:li:person:"):
        print("[linkedin] URN is invalid after sanitization; skipping publish.")
        return False
    print(f"[linkedin] author URN: {LINKEDIN_PERSON_URN}")
    return True


def upload_image_to_linkedin(banner_path):
    init_url = "https://api.linkedin.com/rest/images?action=initializeUpload"
    init_payload = {"initializeUploadRequest": {"owner": LINKEDIN_PERSON_URN}}
    resp = requests.post(init_url, headers=LINKEDIN_HEADERS, json=init_payload, timeout=60)
    if resp.status_code not in (200, 201):
        print(f"[linkedin] image init failed ({resp.status_code}): {resp.text[:400]}")
        return None
    data = resp.json().get("value", {})
    upload_url = data.get("uploadUrl")
    image_urn = data.get("image")
    if not upload_url or not image_urn:
        print(f"[linkedin] unexpected init response: {resp.text[:400]}")
        return None
    with open(banner_path, "rb") as f:
        up_resp = requests.put(
            upload_url,
            headers={"Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}", "Content-Type": "application/octet-stream"},
            data=f, timeout=120,
        )
    if up_resp.status_code not in (200, 201, 204):
        print(f"[linkedin] image upload failed ({up_resp.status_code}): {up_resp.text[:400]}")
        return None
    print(f"[linkedin] image uploaded: {image_urn}")
    return image_urn


def publish_linkedin(body, banner_path):
    if not validate_linkedin():
        return None
    media = None
    image_urn = upload_image_to_linkedin(banner_path)
    if image_urn:
        media = {
            "id": image_urn,
            "altText": f"Auto-generated banner for {REPO_NAME} PR announcement",
        }

    payload = {
        "author": LINKEDIN_PERSON_URN,
        "commentary": body,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
    }
    if media:
        payload["content"] = {"media": media}

    resp = requests.post("https://api.linkedin.com/rest/posts", headers=LINKEDIN_HEADERS, json=payload, timeout=60)
    if resp.status_code != 201:
        print(f"[linkedin] post failed ({resp.status_code}): {resp.text[:500]}")
        return None
    post_urn = resp.headers.get("x-restli-id")
    print(f"[linkedin] post published: {post_urn}")
    return post_urn


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    print(f"[pipeline] AUTO_PUBLISH={AUTO_PUBLISH}")

    pr = get_recent_pr()
    ctx = get_topic_context(pr)
    print(f"[pipeline] content: {ctx['mode']} - {ctx['title']} -> {ctx['html_url']}")

    body = generate_post(ctx)
    if not body:
        print("ERROR: draft generation failed across all free models; aborting.")
        sys.exit(1)

    body = append_footer(body, ctx)

    print("\n==================== GENERATED LINKEDIN POST ====================\n")
    print(body)
    print("\n==================================================================\n")

    passed, reasons = run_automated_audit(body, ctx["html_url"])
    if not passed:
        print("[pipeline] Audit FAILED. Aborting LinkedIn publishing cleanly.")
        dispatch_discord_warning(reasons, body)
        sys.exit(0)

    print("[pipeline] Audit PASSED. Proceeding to banner + publishing stage.")
    banner_path = generate_banner(ctx)

    if AUTO_PUBLISH == "true":
        print("[pipeline] AUTO_PUBLISH=true; publishing to LinkedIn...")
        post_urn = publish_linkedin(body, banner_path)
        if post_urn:
            print(f"[pipeline] Publish complete: {post_urn}")
            dispatch_discord_success(post_urn, banner_path, body)
        else:
            print("[pipeline] Publish FAILED; dispatching warning.")
            dispatch_discord_warning(
                ["LinkedIn publish call failed (see logs above)."], body
            )
            sys.exit(1)
    else:
        print("[pipeline] AUTO_PUBLISH not enabled; dispatching draft to Discord.")
        dispatch_discord(body, banner_path)


if __name__ == "__main__":
    main()