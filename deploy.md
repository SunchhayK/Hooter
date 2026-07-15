# Deploy Bot to Remote Server

## Prerequisites

- Server (Ubuntu/Debian)
- Docker & Docker Compose installed
- Git installed

## Steps

1. **SSH to server:**

   ```bash
   ssh user@server_ip
   ```

2. **Clone repo:**

   ```bash
   git clone git@github.com:SunchhayK/Hooter.git
   cd Hooter
   ```

3. **Setup environment:**

   ```bash
   cp .env.example .env
   nano .env
   # Set environment variables
   ```

4. **Add Google credentials:**

   ```bash
   nano credentials.json
   # Paste credentials.json content
   ```

5. **Start bot:**

   ```bash
   docker-compose up -d --build
   ```

6. **Check logs:**
   ```bash
   docker-compose logs -f bot
   ```

## Update Bot

1. **Pull changes:**

   ```bash
   git pull origin main
   ```

2. **Restart container:**
   ```bash
   docker-compose up -d --build
   ```
