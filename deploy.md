# Deploy Bot to Home Server

## Prerequisites

- Ubuntu/Debian server with Docker & Docker Compose installed
- Domain pointing to your server's public IP (`hooter.labyrinth.buzz.com`)
- Port **80** and **443** open on your router/firewall (forwarded to this machine)
- Git installed

## First-Time Setup

### 1. Clone repo

```bash
git clone git@github.com:SunchhayK/Hooter.git
cd Hooter
```

### 2. Configure environment

```bash
cp .env.example .env
nano .env
# Fill in: TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS,
#          GEMINI_API_KEY, GOOGLE_REDIRECT_URI (already set to domain)
```

### 3. Add Google credentials

```bash
nano credentials.json
# Paste your Web Application credentials.json content
```

### 4. Obtain TLS certificate (first time only)

```bash
# Update certbot email in docker-compose.yml first, then:
docker compose run --rm certbot

# Nginx needs to be up for the ACME challenge:
docker compose up -d nginx
docker compose run --rm certbot
docker compose down
```

### 5. Start all services

```bash
docker compose up -d --build
docker compose logs -f bot
```

### 6. Authorize Google Calendar

Send `/auth` in Telegram. Click the link — Google redirects to
`https://hooter.labyrinth.buzz.com/oauth/callback` automatically.
Bot will notify you in Telegram on success. ✅

---

## Renew TLS Certificate

Certbot certs expire every 90 days. Renew with:

```bash
docker compose run --rm certbot renew
docker compose exec nginx nginx -s reload
```

Add to crontab for automatic renewal:

```cron
0 3 * * * cd /path/to/Hooter && docker compose run --rm certbot renew && docker compose exec nginx nginx -s reload
```

---

## Update Bot

```bash
git pull origin main
docker compose up -d --build bot
```
