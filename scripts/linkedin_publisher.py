import os
import requests

raw_token = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
raw_urn = os.getenv("LINKEDIN_PERSON_URN", "")

# Sanitize inputs: remove newlines, carriage returns, leading/trailing whitespace and surrounding quotes
LINKEDIN_ACCESS_TOKEN = raw_token.strip().strip("'\"").replace("\r", "").replace("\n", "")
LINKEDIN_PERSON_URN = raw_urn.strip().strip("'\"").replace("\r", "").replace("\n", "")

# Ensure the URN has the correct urn:li:person: prefix
if LINKEDIN_PERSON_URN and not LINKEDIN_PERSON_URN.startswith("urn:li:person:"):
    LINKEDIN_PERSON_URN = f"urn:li:person:{LINKEDIN_PERSON_URN}"

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
    if not LINKEDIN_ACCESS_TOKEN or not LINKEDIN_PERSON_URN:
        print("Error: LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN is missing or empty.")
        exit(1)
        
    print(f"Author URN: {LINKEDIN_PERSON_URN}")
    test_text = "Building automated data engines: verifying zero-maintenance technical pipelines via duck-diff and GitHub Actions."
    test_comment = "Repo & pipeline code: https://github.com/AhmadBilalDSA/duck-diff"
    
    urn = publish_post(test_text)
    if urn:
        publish_comment(urn, test_comment)