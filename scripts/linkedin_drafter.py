cat << 'EOF' > scripts/linkedin_drafter.py
import os
import requests
from datetime import datetime, timedelta, timezone

GITHUB_TOKEN = os.getenv("GH_PAT", os.getenv("GITHUB_TOKEN"))
GITHUB_USERNAME = "AhmadBilalDSA"
AI_API_KEY = os.getenv("AI_API_KEY")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1")
AI_MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def get_recent_merged_prs():
    url = f"https://api.github.com/search/issues?q=author:{GITHUB_USERNAME}+is:pr+is:merged"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    recent = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for item in items:
        closed_at = datetime.fromisoformat(item["closed_at"].replace("Z", "+00:00"))
        if closed_at >= cutoff:
            recent.append({
                "title": item["title"],
                "html_url": item["html_url"],
                "body": (item.get("body") or "")[:500],
                "repository_url": item["repository_url"].split("/")[-1]
            })
    return recent

def generate_draft(pr):
    system_prompt = (
        "You are an Analytics Engineering content strategist following 2026 conversion patterns. "
        "Rules: first-person, number-first hook, no corporate cliches, safe triple density with metrics, "
        "and an anchored systems question. Return [POST BODY] followed by [FIRST COMMENT]."
    )
    user_prompt = f"Draft post for PR:\nRepo: {pr['repository_url']}\nTitle: {pr['title']}\nURL: {pr['html_url']}\nDetails: {pr['body']}"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        "temperature": 0.4
    }
    resp = requests.post(f"{AI_BASE_URL}/chat/completions", headers=headers, json=payload)
    return resp.json()["choices"][0]["message"]["content"] if resp.status_code == 200 else None

def dispatch(text):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": f"**New LinkedIn Post Draft Ready:**\n\n{text}"})
    else:
        print("\n--- GENERATED DRAFT ---\n", text)

def main():
    prs = get_recent_merged_prs()
    if not prs:
        prs = [{
            "title": "Boundary assertions in VectorStore.add_texts",
            "html_url": "https://github.com/langchain-ai/langchain/pull/40079",
            "body": "Defensive length check len(ids) == len(texts)",
            "repository_url": "langchain"
        }]
    draft = generate_draft(prs[0])
    if draft:
        dispatch(draft)

if __name__ == "__main__":
    main()
EOF
