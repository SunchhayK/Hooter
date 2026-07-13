import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

# Allow insecure transport (HTTP) for local testing / oauth exchange
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main():
    credentials_file = "credentials.json"
    token_file = "token.json"

    if not os.path.exists(credentials_file):
        print(f"Error: '{credentials_file}' not found.")
        print("\nTo set up Google Calendar access:")
        print("1. Go to Google Cloud Console (https://console.cloud.google.com/)")
        print("2. Create a project and enable 'Google Calendar API'")
        print("3. Configure the OAuth Consent Screen (external, add test user)")
        print("4. Go to Credentials -> Create Credentials -> OAuth client ID")
        print("5. Set application type to 'Desktop app'")
        print(
            "6. Download JSON file, rename to 'credentials.json', and place in this directory."
        )
        sys.exit(1)

    print("Choose authentication method:")
    print(
        "1) Automatic (starts a local webserver to capture login, best for local machine)"
    )
    print("2) Manual (useful if running on a remote server/headless environment)")

    choice = input("Enter choice (1 or 2): ").strip()

    if choice == "1":
        print("Starting Google OAuth local server...")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_file, "w") as token:
                token.write(creds.to_json())
            print(f"\nSuccess! Credentials saved to '{token_file}'.")
        except Exception as e:
            print(f"\nOAuth flow failed: {e}")
            sys.exit(1)
    else:
        # Use http://localhost (matches credentials.json default)
        flow = InstalledAppFlow.from_client_secrets_file(
            credentials_file, scopes=SCOPES, redirect_uri="http://localhost"
        )
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")

        print("\n1. Visit this URL in your browser to log in:")
        print(auth_url)
        print(
            "\n2. After authorizing, your browser will redirect to a broken page (e.g., http://localhost/?state=...&code=...)"
        )
        print(
            "3. IMPORTANT: Copy the FULL redirect URL (including '?state=...&code=...') from your browser's address bar and paste it below."
        )

        redirect_url = input("\nPaste full redirect URL: ").strip()

        # Validation checks on user input
        if "code_challenge=" not in redirect_url:
            print("\n❌ Error: The URL pasted is missing the '?code=' parameter.")
            print(
                "Make sure you complete the sign-in and copy the ENTIRE URL from the browser address bar."
            )
            sys.exit(1)

        try:
            # Exchange redirect code for credentials
            flow.fetch_token(authorization_response=redirect_url)
            creds = flow.credentials
            with open(token_file, "w") as token:
                token.write(creds.to_json())
            print(f"\nSuccess! Credentials saved to '{token_file}'.")
        except Exception as e:
            print(f"\nFailed to fetch credentials: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
