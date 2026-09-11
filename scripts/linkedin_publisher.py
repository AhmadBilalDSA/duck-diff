import os
import requests

LINKEDIN_ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN")
LINKEDIN_PERSON_URN = os.getenv("LINKEDIN_PERSON_URN")  # Format: urn:li:person:XXXX

API_HEADERS = {
    "Authorization": f"Bearer {LINKEDIN_ACCESS_TOKEN}",
    "Content-Type": "application/json",
    "LinkedIn-Version": "202601",
    "X-Restli-Protocol-Version": "2.0.0"
}

def publish_post(text: str) -> str:
    """Publishes a text post directly to personal feed."""
    url = "https://api.linkedin.com/rest/posts"
    payload = {
        "author": LINKEDIN_PERSON_URN,
        "commentary": text,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": []
        },
        "lifecycleState": "PUBLISHED"
    }
    resp = requests.post(url, headers=API_HEADERS, json=payload)
    if resp.status_code == 201:
        urn = resp.headers.get("x-restli-id")
        print(f"Successfully published post to LinkedIn! Post URN: {urn}")
        return urn
    else:
        print(f"LinkedIn publish failed ({resp.status_code}): {resp.text}")
        return None

def publish_comment(post_urn: str, comment_text: str):
    """Adds first comment to preserve feed reach."""
    encoded_urn = requests.utils.quote(post_urn)
    url = f"https://api.linkedin.com/rest/socialActions/{encoded_urn}/comments"
    payload = {
        "actor": LINKEDIN_PERSON_URN,
        "message": {"text": comment_text}
    }
    resp = requests.post(url, headers=API_HEADERS, json=payload)
    if resp.status_code == 201:
        print("First comment placed successfully!")
    else:
        print(f"Comment failed ({resp.status_code}): {resp.text}")

if __name__ == "__main__":
    test_text = "Building automated data engines: verifying zero-maintenance technical pipelines via duck-diff and GitHub Actions."
    test_comment = "Repo & pipeline code: https://github.com/AhmadBilalDSA/duck-diff"
    urn = publish_post(test_text)
    if urn:
        publish_comment(urn, test_comment)