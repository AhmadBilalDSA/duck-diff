import io
import os
import re
import struct
import sys
import requests
from datetime import datetime, timedelta, timezone

GITHUB_USERNAME = "AhmadBilalDSA"
REPO_NAME = "duck-diff"
REPO_URL = f"https://github.com/{GITHUB_USERNAME}/{REPO_NAME}"

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
REQUIRED_HASHTAGS = ["#DataEngineering", "#Python", "#SystemsEngineering", "#DatabaseInternals"]
HASHTAGS_LINE = " ".join(REQUIRED_HASHTAGS)
MIN_BODY_CHARS, MAX_BODY_CHARS = 500, 2000
RECENT_PR_DAYS = 7
PR_SEARCH_URL = (
    f"https://api.github.com/search/issues?q=author:{GITHUB_USERNAME}+is:pr"
    "&sort=updated&order=desc&per_page=50"
)

# Beginner / meta repos that add no signal to a technical skills extractor.
BLACKLISTED_REPOS = {"first-contributions", "firstcontributions", "contribute-to-this-project"}
BACKLIST_SUBSTRINGS = ("first-contribution", "first contribution")


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
# Skill taxonomy & multi-repo portfolio catalog
# ---------------------------------------------------------------------------
SKILL_TAXONOMY = {
    "AST Parsing": ["ast", "parser", "parse", "syntax", "grammar", "dialect", "lint", "token"],
    "Columnar Engines": ["columnar", "arrow", "polar", "parquet", "vectorized", "dataframe", "schema"],
    "Query Hardening": ["injection", "parameterized", "sanitiz", "escap", "bind", "prepared"],
    "Distributed SQL": ["distributed", "tidb", "transaction", "raft", "storage engine", "query plan", "planning"],
    "Type Reflection": ["type", "dtype", "invariant", "reflection", "cast", "typing"],
    "Data Quality": ["validation", "quality", "assertion", "expectation", "guardrail", "test suite"],
    "In-Memory Reconciliation": ["reconciliation", "reconcile", "in-memory", "drift", "memory footprint", "diff"],
    "Agentic Workflows": ["agent", "tool", "semantic", "retrieval", "routing", "workflow"],
}

# Portfolio catalog used for rotation when no external PR was updated within 7 days.
# Each entry is a real repo in Ahmad's contribution portfolio.
PORTFOLIO_CATALOG = [
    {
        "repo": "semantica-agi/semantica",
        "code_url": "https://github.com/semantica-agi/semantica",
        "title": "Routing agentic retrievals: tool-selection latency vs answer recall",
        "skills": ["Agentic Workflows", "Semantic Retrieval", "Tool Routing"],
        "brief": (
            "Agentic workflows that route semantic retrieval through tool selection. "
            "Trade-offs between router latency, retrieval precision, and the safety of "
            "letting a model decide which tool owns a query before context is assembled."
        ),
    },
    {
        "repo": "sqlfluff/sqlfluff",
        "code_url": "https://github.com/sqlfluff/sqlfluff",
        "title": "Parsing SQL dialects: grammar classification, AST nodes, lint throughput",
        "skills": ["AST Parsing", "Dialect Grammar", "Syntax Analysis"],
        "brief": (
            "SQLFluff classifies dialect grammar into AST nodes. Parsing strategy trades "
            "off backtracking depth against correctness for dialect-specific syntax, and "
            "the linter converts parse trees into actionable violations under throughput pressure."
        ),
    },
    {
        "repo": "ibis-project/ibis",
        "code_url": "https://github.com/ibis-project/ibis",
        "title": "Reflecting backend schemas into portable columnar type systems",
        "skills": ["Columnar Engines", "Type Reflection", "Polars Integration"],
        "brief": (
            "Ibis reflects backend schemas so one expression graph compiles across engines. "
            "Columnar type reflection must preserve dtype invariants when pushing "
            "Polars-backed execution through the same front-end contract."
        ),
    },
    {
        "repo": "sara-czasak/py-simple-wrap",
        "code_url": "https://github.com/sara-czasak/py-simple-wrap",
        "title": "Fighting SQL injection with parameterized wrapping, not string soup",
        "skills": ["Query Hardening", "Parameterized Execution", "Sanitization"],
        "brief": (
            "Parameterized wrapping that keeps user input out of the SQL grammar. "
            "Sanitization cost, prepared-statement reuse, and the failure modes you "
            "avoid when identifiers and values are bound instead of interpolated."
        ),
    },
    {
        "repo": "pingcap/tidb",
        "code_url": "https://github.com/pingcap/tidb",
        "title": "Distributed transactions: planner decisions vs storage-engine guarantees",
        "skills": ["Distributed SQL", "Query Planning", "Storage Engines"],
        "brief": (
            "TiDB splits query planning from storage-engine execution. Distributed "
            "transactions force the planner to reason about region boundaries, commit "
            "ordering, and the durability guarantees the engine can actually honor."
        ),
    },
    {
        "repo": "great-expectations/great_expectations",
        "code_url": "https://github.com/great-expectations/great_expectations",
        "title": "Data quality as code: validation suites that fail pipelines fast",
        "skills": ["Data Quality", "Validation Suites", "Data Assertions"],
        "brief": (
            "Automated validation suites turn data quality into assertions the pipeline "
            "must satisfy before downstream consumers touch a row, converting silent "
            "corruption into a loud, cheap failure at the earliest checkpoint."
        ),
    },
    {
        "repo": f"{GITHUB_USERNAME}/{REPO_NAME}",
        "code_url": REPO_URL,
        "title": "Reconciling datasets in memory under a constant memory footprint",
        "skills": ["In-Memory Reconciliation", "Data Drift Detection", "Constant Memory"],
        "brief": (
            "duck-diff reconciles two datasets entirely in memory while holding the "
            "footprint constant regardless of input size, then fingerprints drift so "
            "schema and data changes surface as explicit, testable invariants."
        ),
    },
]


def _is_blacklisted(repo_name):
    lowered = (repo_name or "").lower()
    if lowered in BLACKLISTED_REPOS:
        return True
    return any(sub in lowered for sub in BACKLIST_SUBSTRINGS)


def infer_skills(repo_name, title, body):
    """Map a repo + PR text onto core competencies from the skill taxonomy."""
    haystack = f"{repo_name} {title} {body}".lower()
    skills = []
    for skill, keywords in SKILL_TAXONOMY.items():
        if any(keyword in haystack for keyword in keywords):
            skills.append(skill)
    if not skills:
        skills = ["Systems Engineering"]
    return skills


def catalog_skills(repo_name):
    for entry in PORTFOLIO_CATALOG:
        if entry["repo"].lower() == (repo_name or "").lower():
            return entry["skills"]
    return None


# ---------------------------------------------------------------------------
# Step 1: Dynamic GitHub PR discovery (open, review-pending, and merged)
# ---------------------------------------------------------------------------
def fetch_latest_merged_pr():
    """Return the most recently updated PR across Ahmad's portfolio.

    The search index covers open, review-pending, and merged pull requests;
    the first item updated within the 7-day window (excluding blacklisted
    beginner/meta repos) wins. Skills are inferred from repo + PR text.
    """
    headers = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github+json"} if GH_PAT else {}
    resp = requests.get(PR_SEARCH_URL, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"[github] search failed ({resp.status_code}): {resp.text}")
        return None
    items = resp.json().get("items", [])
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_PR_DAYS)
    for item in items:
        repo_full = (item.get("repository_url") or "").replace("https://api.github.com/repos/", "")
        if not repo_full or _is_blacklisted(repo_full):
            print(f"[github] skipping blacklisted/beginner repo: {repo_full or 'unknown'}")
            continue
        updated_at = item.get("updated_at") or item.get("created_at")
        if not updated_at:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if updated_dt < cutoff:
            continue
        title = item.get("title") or "Untitled pull request"
        body = (item.get("body") or "")[:800]
        return {
            "mode": "PR",
            "state": _classify_pr(item),
            "title": title,
            "body": body or "No PR description provided.",
            "repository_url": repo_full,
            "html_url": item.get("html_url") or f"https://github.com/{repo_full}/pull/{item.get('number', 0)}",
            "number": item.get("number", 0),
            "skills": infer_skills(repo_full, title, body),
            "updated_at": updated_dt.isoformat(),
        }
    return None


def _classify_pr(item):
    pr_meta = item.get("pull_request") or {}
    if pr_meta.get("merged_at"):
        return "merged"
    if (item.get("state") or "").lower() == "open":
        return "open"
    return "review_pending"


def get_recent_pr():
    try:
        pr = fetch_latest_merged_pr()
        if pr:
            print(f"[github] PR selected [{pr['state']}]: {pr['repository_url']} #{pr['number']} ({pr['title']})")
        return pr
    except Exception as e:
        print(f"[github] error querying API: {e}")
        return None


# ---------------------------------------------------------------------------
# Step 1b: Rotation engine (portfolio catalog, deterministic by UTC weekday)
# ---------------------------------------------------------------------------
def get_topic_context(pr):
    if pr:
        print(f"[topic] external PR updated within last {RECENT_PR_DAYS} days; using multi-repo skills.")
        return pr

    weekday = datetime.now(timezone.utc).weekday()
    entry = PORTFOLIO_CATALOG[weekday % len(PORTFOLIO_CATALOG)]
    print(f"[topic] no external PR within {RECENT_PR_DAYS} days; rotating portfolio "
          f"(UTC weekday {weekday}): {entry['repo']}")
    return {
        "mode": "TOPIC",
        "title": entry["title"],
        "body": entry["brief"],
        "repository_url": entry["repo"],
        "html_url": entry["code_url"],
        "number": None,
        "skills": entry["skills"],
        "updated_at": None,
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
# Step 3: AI post drafting (skill-tagged, audit-ready)
# ---------------------------------------------------------------------------
def generate_post(ctx):
    if not AI_API_KEY:
        print("ERROR: AI_API_KEY is missing or empty.")
        return None

    skills = ", ".join(ctx.get("skills") or [])
    system_prompt = (
        "You are a senior Systems Engineer writing high-conversion LinkedIn posts "
        "for a technical audience. Follow these rules exactly:\n"
        "1. Hook: the FIRST line must open with a concrete numeric metric or a "
        "system invariant (latency ms, memory footprint MB, row count, throughput, "
        "commit ordering, byte-level buffers).\n"
        "2. Write exactly 3 technical density points that weave in the extracted "
        "engineering skills and their code-safety trade-offs (what you gained vs "
        "what you defended against).\n"
        "3. Thread natural semantic keywords throughout for search discovery.\n"
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
        f"Extracted skills to feature: {skills}\n"
        f"Target 700-1200 characters for substance and LinkedIn algorithm favor."
    )
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": REPO_URL,
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
def run_automated_audit(body, reference_url, skills=None):
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
    if skills:
        mentioned = [s for s in skills if s.lower() in lower_body]
        print(f"[audit] extracted-skills mentioned in body: {mentioned or 'NONE (informational)'}")
    passed = len(reasons) == 0
    if passed:
        print(f"[audit] PASS - body {body_len} chars, numeric hook, cliches 0, SEO footer OK.")
    else:
        print(f"[audit] FAIL - {len(reasons)} issue(s):")
        for reason in reasons:
            print(f"  - {reason}")
    return passed, reasons


# ---------------------------------------------------------------------------
# Step 4: Banner generation with matplotlib (headless Agg backend)
# ---------------------------------------------------------------------------
def create_banner_figure(ctx):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor("#0f172a")
    ax.set_facecolor("#0f172a")
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 6.75)
    ax.axis("off")
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

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
    ax.text(0.6, 2.0, "Automated technical storytelling  powered by GitHub Actions + duck-diff",
            color="#38bdf8", fontsize=13, va="center", family="monospace")
    ax.plot([0.6, 11.4], [1.35, 1.35], color="#334155", lw=2)
    ax.text(0.6, 0.6, "github.com/" + GITHUB_USERNAME + "/" + REPO_NAME,
            color="#64748b", fontsize=12, va="center", family="monospace")
    return fig


def generate_banner(ctx):
    import matplotlib.pyplot as plt

    fig = create_banner_figure(ctx)
    fig.savefig(BANNER_PATH, facecolor="#0f172a")
    plt.close(fig)
    print(f"[banner] generated {BANNER_PATH}")
    return BANNER_PATH


def render_banner_buffer(ctx):
    """Render the 1200x675 banner to an in-memory PNG buffer (headless)."""
    import matplotlib.pyplot as plt

    fig = create_banner_figure(ctx)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="#0f172a")
    plt.close(fig)
    data = buf.getvalue()
    buf.close()
    return data


def _png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a valid PNG header"
    return struct.unpack(">II", data[16:24])


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
        print("[discord] no webhook URL configured; warning not dispatched.")
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


def dispatch_discord_success(post_urn, banner_path, body, skills):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] no webhook URL configured; success not dispatched.")
        return
    print("[discord] dispatching publish confirmation to webhook...")
    try:
        skills_line = ", ".join(skills) if skills else "no explicit skills tagged"
        message = (
            f"**Publish confirmed - live on LinkedIn.**\n\n"
            f"Post URN: `{post_urn}`\n"
            f"Live URL: https://www.linkedin.com/feed/update/{post_urn}\n"
            f"Extracted skills: {skills_line}\n"
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
# Step 6: LinkedIn publishing (no restricted comments endpoint)
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


def build_post_payload(body, image_urn=None):
    """Single-object media schema: content.media is a list with one {id} entry."""
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
    if image_urn:
        payload["content"] = {"media": [{"id": image_urn}]}
    return payload


def publish_linkedin(body, banner_path):
    if not validate_linkedin():
        return None
    image_urn = upload_image_to_linkedin(banner_path)
    payload = build_post_payload(body, image_urn=image_urn)

    resp = requests.post("https://api.linkedin.com/rest/posts", headers=LINKEDIN_HEADERS, json=payload, timeout=60)
    if resp.status_code != 201:
        print(f"[linkedin] post failed ({resp.status_code}): {resp.text[:500]}")
        return None
    post_urn = resp.headers.get("x-restli-id")
    print(f"[linkedin] post published: {post_urn}")
    return post_urn


# ---------------------------------------------------------------------------
# Self-test suite
# ---------------------------------------------------------------------------
def _test_sanitize_secret():
    cases = [
        ('"raw_token"\r\n', "raw_token"),
        ("  \t 'token'  ", "token"),
        ('"token"', "token"),
        ("urn:li:person:ABC123", "urn:li:person:ABC123"),
        ('  "urn:li:person:ABC123"  \r\n', "urn:li:person:ABC123"),
        ("\r\nvalue\r\n", "value"),
        ("'single'", "single"),
    ]
    ok = True
    for raw, expected in cases:
        got = sanitize_secret(raw)
        if got != expected:
            ok = False
            print(f"    FAIL: sanitize_secret({raw!r}) -> {got!r}, expected {expected!r}")
    if sanitize_secret("'urn:li:person:XYZ-420'" ) != "urn:li:person:XYZ-420":
        ok = False
        print("    FAIL: urn:li:person: prefix not preserved through sanitization")
    return ok


def _test_audit_engine():
    url = "https://github.com/AhmadBilalDSA/duck-diff"
    footer = f"\n\nCode: {url}\n#DataEngineering #Python #SystemsEngineering #DatabaseInternals"
    compliant = (
        "41% lower peak memory from an in-memory reconciliation pass that holds the buffer flat.\n\n"
        "Three density points: constant-memory footprints remove spill-to-disk stalls; "
        "columnar type reflection preserves dtype invariants across engine boundaries; "
        "query hardening keeps untrusted input out of the SQL grammar via parameterized wrapping.\n\n"
        "The trade-off: every byte you keep resident buys latency at the cost of memory ceiling."
    ) + footer
    passed, _ = run_automated_audit(compliant, url, skills=["In-Memory Reconciliation"])
    ok = passed

    no_number = (
        "Our reconciliation pass flattened the heap footprint for large dataset comparisons.\n\n"
        "Three density points covering constant-memory footprints, columnar type reflection, "
        "and query hardening through parameterized wrapping and sanitization trade-offs. "
        "Every resident byte buys latency at the cost of a higher memory ceiling, so the "
        "buffer is sized once and never reallocated while batches stream through the engine. "
        "Schema contracts are asserted at the boundary, drift is fingerprinted per run, and "
        "injections are neutralized before a single token touches the parser.\n\n"
        "The open question: where does your memory ceiling sit?"
    ) + footer
    rejected_1, _ = run_automated_audit(no_number, url)
    if rejected_1:
        ok = False
        print("    FAIL: audit accepted a post with a non-numeric hook.")

    cliche_body = (
        "42% less memory pressure from the flat reconciliation buffer.\n\n"
        "I'm thrilled to share three density points on constant-memory footprints, "
        "columnar type reflection, and query hardening built on parameterized wrapping. "
        "The buffer is sized once and never reallocated while batches stream through the "
        "engine, schema contracts are asserted at the boundary, drift is fingerprinted per "
        "run, and injections are neutralized before a single token touches the parser. "
        "Every resident byte buys latency at the cost of a higher memory ceiling, so the "
        "design favors boundedness over unbounded speed.\n\n"
        "An open systems question: what granularity do you reconcile at?"
    ) + footer
    rejected_2, _ = run_automated_audit(cliche_body, url)
    if rejected_2:
        ok = False
        print("    FAIL: audit accepted a post containing a banned corporate cliche.")
    return ok


def _test_banner_render():
    ctx = {
        "mode": "PR",
        "title": "Test banner: in-memory reconciliation under a constant memory footprint",
        "repository_url": "AhmadBilalDSA/duck-diff",
        "number": 42,
    }
    data = render_banner_buffer(ctx)
    width, height = _png_size(data)
    ok = (width, height) == (1200, 675)
    if not ok:
        print(f"    FAIL: banner rendered {width}x{height}, expected 1200x675")
    return ok, len(data)


def _test_media_schema():
    payload = build_post_payload("Some commentary", image_urn="urn:li:image:CxT-TEST")
    media = payload.get("content", {}).get("media")
    ok = media == [{"id": "urn:li:image:CxT-TEST"}]
    if not ok:
        print(f"    FAIL: content.media = {media!r}, expected [{{'id': image_urn}}]")
    return ok


def run_pipeline_tests():
    print("=" * 60)
    print("RUNNING SELF-TEST SUITE")
    print("=" * 60)
    results = [
        ("Test 1: sanitize_secret strips quotes/whitespace and preserves urn:li:person:", _test_sanitize_secret()),
        ("Test 2: audit engine gates numeric-hook and banned-cliche failures", _test_audit_engine()),
        ("Test 3: headless matplotlib banner renders 1200x675 PNG buffer", _test_banner_render()[0]),
        ("Test 4: post payload uses content.media = [{'id': image_urn}] schema", _test_media_schema()),
    ]
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    all_ok = all(ok for _, ok in results)
    print("RESULT: ALL TESTS PASSED" if all_ok else "RESULT: TESTS FAILED")
    return all_ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _tests_only_requested():
    return os.getenv("RUN_TESTS_ONLY", "").strip().lower() == "true" or "--test-only" in sys.argv


def main():
    print(f"[pipeline] AUTO_PUBLISH={AUTO_PUBLISH}")

    if _tests_only_requested():
        sys.exit(0 if run_pipeline_tests() else 1)

    if not run_pipeline_tests():
        print("[pipeline] SELF-TEST FAILED: aborting before any fetch or publish.")
        dispatch_discord_warning(
            ["Self-test suite failed; pipeline aborted before GitHub fetch and LinkedIn publish."],
            "No draft generated."
        )
        sys.exit(1)

    pr = get_recent_pr()
    ctx = get_topic_context(pr)
    skills = ctx.get("skills") or []
    print(f"[pipeline] content: {ctx['mode']} - {ctx['title']} -> {ctx['html_url']}")
    print(f"[pipeline] skills tagged: {', '.join(skills)}")

    body = generate_post(ctx)
    if not body:
        print("ERROR: draft generation failed across all free models; aborting.")
        sys.exit(1)

    body = append_footer(body, ctx)

    print("\n==================== GENERATED LINKEDIN POST ====================\n")
    print(body)
    print("\n==================================================================\n")

    passed, reasons = run_automated_audit(body, ctx["html_url"], skills=skills)
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
            dispatch_discord_success(post_urn, banner_path, body, skills)
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