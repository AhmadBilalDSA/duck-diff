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

# Content audit guardrails
BANNED_CLICHES = ["thrilled", "excited to share", "humbled", "delighted to announce"]

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
# Step 1: GitHub PR discovery
# ---------------------------------------------------------------------------
def fetch_latest_merged_pr():
    url = f"https://api.github.com/search/issues?q=author:{GITHUB_USERNAME}+is:pr+is:merged&per_page=5&sort=updated&order=desc"
    headers = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github+json"} if GH_PAT else {}
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"[github] search failed ({resp.status_code}): {resp.text}")
        return None
    items = resp.json().get("items", [])
    if not items:
        return None
    item = items[0]
    repo_full = (item.get("repository_url") or "").replace("https://api.github.com/repos/", "") or REPO_NAME
    pr = {
        "title": item["title"],
        "html_url": item["html_url"],
        "body": (item.get("body") or "")[:800],
        "repository_url": repo_full,
        "number": item.get("number", 0),
        "closed_at": item.get("closed_at", ""),
    }
    print(f"[github] fetched latest merged PR #{pr['number']}: {pr['title']}")
    return pr


def fallback_pr():
    print("[github] no merged PRs found; using fallback PR template.")
    return {
        "title": "Boundary assertions in VectorStore.add_texts",
        "html_url": "https://github.com/langchain-ai/langchain/pull/40079",
        "body": "Defensive length check len(ids) == len(texts)",
        "repository_url": "langchain",
        "number": 40079,
        "closed_at": "",
    }


def get_pr():
    try:
        pr = fetch_latest_merged_pr()
    except Exception as e:
        print(f"[github] error querying API: {e}")
        pr = None
    return pr if pr else fallback_pr()


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
def generate_post(pr):
    if not AI_API_KEY:
        print("ERROR: AI_API_KEY is missing or empty.")
        return None, None

    system_prompt = (
        "You are an Analytics Engineering content strategist writing LinkedIn posts "
        "for a technical founder. Follow these conversion patterns exactly:\n"
        "1. Number-first hook: open with a concrete number/metric.\n"
        "2. Exactly 3 technical density points: dense, metrics-driven, zero corporate cliches.\n"
        "3. Anchored open question at the end tied to the post subject.\n"
        "4. First-person voice, no emojis, no hashtags.\n"
        "Return ONLY two sections separated by blank lines:\n"
        "[POST BODY]\n<post text>\n\n[FIRST COMMENT]\n<first comment text>"
    )
    user_prompt = (
        f"Draft a LinkedIn post for this PR:\n"
        f"Repo: {pr['repository_url']}\nTitle: {pr['title']}\nURL: {pr['html_url']}\n"
        f"Details: {pr['body']}"
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
                return parse_post(text, pr)
            print(f"[ai] model {model} failed ({resp.status_code}): {resp.text[:300]}")
        except Exception as e:
            print(f"[ai] model {model} error: {e}")
    return None, None


def parse_post(text, pr):
    body_match = re.search(r"\[POST BODY\]\s*(.*?)(?=\[FIRST COMMENT\]|$)", text, re.S)
    comment_match = re.search(r"\[FIRST COMMENT\]\s*(.*)", text, re.S)
    body = (body_match.group(1) if body_match else text).strip()
    comment = (comment_match.group(1) if comment_match else None) or pr["html_url"].strip()
    return body, comment


# ---------------------------------------------------------------------------
# Step 3b: Automated content audit engine
# ---------------------------------------------------------------------------
def run_automated_audit(body, comment):
    reasons = []
    body_len = len(body or "")
    if not (120 <= body_len <= 2800):
        reasons.append(
            f"Body length {body_len} chars is outside the 120-2800 character range."
        )
    lower_body = (body or "").lower()
    cliches_found = [c for c in BANNED_CLICHES if c in lower_body]
    if cliches_found:
        reasons.append(
            f"Banned corporate clichés detected: {', '.join(cliches_found)}."
        )
    hook = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    if not re.search(r"\d+", hook):
        reasons.append(
            "Hook (first sentence) contains no concrete number or metric (regex r'\\d+')."
        )
    passed = len(reasons) == 0
    if passed:
        print(f"[audit] PASS — body {body_len} chars, numeric hook, no corporate clichés.")
    else:
        print(f"[audit] FAIL — {len(reasons)} issue(s):")
        for reason in reasons:
            print(f"  - {reason}")
    return passed, reasons


# ---------------------------------------------------------------------------
# Step 4: Banner generation with matplotlib
# ---------------------------------------------------------------------------
def generate_banner(pr):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=120)
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.75)
    ax.axis("off")

    title = pr["title"]
    if len(title) > 70:
        title = title[:67].rsplit(" ", 1)[0] + "..."
    ax.text(0.6, 4.7, f"PR #{pr.get('number', '')}  //  {pr['repository_url']}",
            color="#94a3b8", fontsize=13, va="center", family="monospace")
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
def dispatch_discord(body, comment, banner_path):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; skipping dispatch.")
        return
    print("[discord] dispatching post + banner to webhook...")
    try:
        message = f"**New technical post ready for {REPO_NAME}:**\n\n{body[:1500]}\n\n```{comment[:300]}```"
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


def dispatch_discord_warning(reasons, body, comment):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; audit failure not dispatched.")
        return
    print("[discord] dispatching audit-failure warning to webhook...")
    try:
        message = (
            f"**AUDIT FAILED - post NOT published to LinkedIn.**\n\n"
            f"Failure reasons:\n" + "\n".join(f"- {r}" for r in reasons) +
            f"\n\n**Draft:**\n{body[:1500]}\n\n```{comment[:300]}```"
        )
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": message}, timeout=60)
        if resp.status_code in (200, 204):
            print("[discord] audit-failure warning delivered.")
        else:
            print(f"[discord] warning webhook failed ({resp.status_code}): {resp.text[:300]}")
    except Exception as e:
        print(f"[discord] error sending warning: {e}")


def dispatch_discord_success(post_urn, banner_path):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; success not dispatched.")
        return
    print("[discord] dispatching publish confirmation to webhook...")
    try:
        message = (
            f"**Publish confirmed - live on LinkedIn.**\n\n"
            f"Post URN: `{post_urn}`\n"
            f"Preview: https://www.linkedin.com/feed/update/{post_urn}"
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


def publish_linkedin(body, comment, banner_path):
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
    if post_urn and comment:
        place_first_comment(post_urn, comment)
    return post_urn


def place_first_comment(post_urn, comment):
    encoded_urn = requests.utils.quote(post_urn)
    url = f"https://api.linkedin.com/rest/socialActions/{encoded_urn}/comments"
    payload = {"actor": LINKEDIN_PERSON_URN, "message": {"text": comment}}
    resp = requests.post(url, headers=LINKEDIN_HEADERS, json=payload, timeout=60)
    if resp.status_code == 201:
        print("[linkedin] first comment placed successfully.")
    else:
        print(f"[linkedin] comment failed ({resp.status_code}): {resp.text[:400]}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def main():
    print(f"[pipeline] AUTO_PUBLISH={AUTO_PUBLISH}")

    pr = get_pr()
    print(f"[pipeline] PR: {pr['title']} -> {pr['html_url']}")

    body, comment = generate_post(pr)
    if not body:
        print("ERROR: draft generation failed across all free models; aborting.")
        sys.exit(1)

    print("\n==================== GENERATED LINKEDIN POST ====================\n")
    print("[POST BODY]\n" + body + "\n\n[FIRST COMMENT]\n" + comment)
    print("\n==================================================================\n")

    passed, reasons = run_automated_audit(body, comment)
    if not passed:
        print("[pipeline] Audit FAILED. Aborting LinkedIn publishing cleanly.")
        dispatch_discord_warning(reasons, body, comment)
        sys.exit(0)

    print("[pipeline] Audit PASSED. Proceeding to banner + publishing stage.")
    banner_path = generate_banner(pr)

    if AUTO_PUBLISH == "true":
        print("[pipeline] AUTO_PUBLISH=true; publishing to LinkedIn...")
        post_urn = publish_linkedin(body, comment, banner_path)
        if post_urn:
            print(f"[pipeline] Publish complete: {post_urn}")
            dispatch_discord_success(post_urn, banner_path)
        else:
            print("[pipeline] Publish FAILED; dispatching warning.")
            dispatch_discord_warning(
                ["LinkedIn publish call failed (see logs above)."], body, comment
            )
            sys.exit(1)
    else:
        print("[pipeline] AUTO_PUBLISH not enabled; dispatching draft to Discord.")
        dispatch_discord(body, comment, banner_path)


if __name__ == "__main__":
    main()