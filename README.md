# Telegram Google Calendar Bot

Bot that parses forwarded scheduling messages using AI (Gemini or OpenAI) and inserts events into Google Calendar.

## Setup Instructions

1. **Install dependencies**:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure Environment**:
   - Copy `.env.example` to `.env`.
   - Set your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` (comma-separated), active AI provider, and the API key for your chosen provider.
   - Configure the target `TIMEZONE` (crucial for relative date parsing).

3. **Set up Google Calendar access**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/).
   - Create a project and enable the **Google Calendar API**.
   - Go to **OAuth consent screen**, set it up (External, add your Google account as a Test user).
   - Go to **Credentials**, click **Create Credentials**, select **OAuth client ID**.
   - Set Application type to **Desktop app**.
   - Download the JSON credentials file, rename it to `credentials.json`, and place it in this directory.

4. **Authenticate Google Account**:
   - Run the helper script to complete OAuth authorization and generate `token.json`:

     ```bash
     python setup_oauth.py
     ```

5. **Test Parser**:
   - Verify mock logic passes:

     ```bash
     python test_parser.py
     ```

6. **Run the Bot**:

   ```bash
   python bot.py
   ```
