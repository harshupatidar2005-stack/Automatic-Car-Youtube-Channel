"""
get_refresh_token.py
----------------------
Run this ONCE, locally, to obtain a long-lived refresh token for your
YouTube channel. This is the one unavoidable manual step in the whole
system -- YouTube requires a human to click "Allow" on the Google consent
screen at least once. After this, uploads run forever without any
further input.

Usage:
  1. Create OAuth credentials (Desktop app type) in Google Cloud Console
     for a project with the YouTube Data API v3 enabled.
  2. Download the client secret JSON, save as client_secret.json here.
  3. python get_refresh_token.py
  4. A browser opens -> log in with the Google account that owns your
     YouTube channel -> approve.
  5. Copy the printed refresh token into your GitHub repo secrets as
     YOUTUBE_REFRESH_TOKEN (along with YOUTUBE_CLIENT_ID / _SECRET from
     the same client_secret.json).
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    creds = flow.run_local_server(port=0)
    print("\n--- SAVE THESE AS GITHUB REPO SECRETS ---")
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("------------------------------------------")


if __name__ == "__main__":
    main()
