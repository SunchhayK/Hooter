"""Setup script for manual Google Calendar OAuth authorization."""

import os
import sys

# Must be set before any google-auth import
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "tokens/token.json"


def _write_token(path: str, json_str: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json_str)


def run_automatic() -> None:
    print("Starting Google OAuth local server...")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        _write_token(TOKEN_FILE, creds.to_json())
        print(f"\nSuccess! Credentials saved to '{TOKEN_FILE}'.")
    except Exception as e:
        print(f"\nOAuth flow failed: {e}")
        sys.exit(1)


def run_manual() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(
        CREDENTIALS_FILE, scopes=SCOPES, redirect_uri="http://localhost"
    )
    auth_url, _ = flow.authorization_url(
        prompt="select_account consent", access_type="offline"
    )

    print("\n1. Visit this URL in your browser to log in:")
    print(auth_url)
    print(
        "\n2. After authorizing, your browser will redirect to a broken page "
        "(e.g., http://localhost/?state=...&code=...)"
    )
    print(
        "3. IMPORTANT: Copy the FULL redirect URL (including '?state=...&code=...') "
        "from your browser's address bar and paste it below."
    )

    redirect_url = input("\nPaste full redirect URL: ").strip()

    if "code=" not in redirect_url:
        print("\n❌ Error: The URL is missing the '?code=' parameter.")
        print(
            "Make sure you complete the sign-in and copy the ENTIRE URL from the browser address bar."
        )
        sys.exit(1)

    try:
        flow.fetch_token(authorization_response=redirect_url)
        _write_token(TOKEN_FILE, flow.credentials.to_json())
        print(f"\nSuccess! Credentials saved to '{TOKEN_FILE}'.")
    except Exception as e:
        print(f"\nFailed to fetch credentials: {e}")
        sys.exit(1)


def main() -> None:
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"Error: '{CREDENTIALS_FILE}' not found.")
        print("\nTo set up Google Calendar access:")
        print("1. Go to Google Cloud Console (https://console.cloud.google.com/)")
        print("2. Create a project and enable 'Google Calendar API'")
        print("3. Configure the OAuth Consent Screen (external, add test user)")
        print("4. Go to Credentials -> Create Credentials -> OAuth client ID")
        print("5. Set application type to 'Desktop app'")
        print(
            "6. Download JSON file, rename to 'credentials.json', and place in the project root."
        )
        sys.exit(1)

    print("Choose authentication method:")
    print("1) Automatic (starts a local webserver, best for local machine)")
    print("2) Manual (useful if running on a remote/headless server)")

    choice = input("Enter choice (1 or 2): ").strip()
    if choice == "1":
        run_automatic()
    else:
        run_manual()


if __name__ == "__main__":
    main()
