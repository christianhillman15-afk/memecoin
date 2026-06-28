# Deploying MemeRadar on DigitalOcean

A memecoin scanner is most useful running **24/7** — always scanning, building
its equity curve, and catching pumps while you're away. The easiest robust way
to do that is a small **DigitalOcean Droplet running Docker Compose** with a
persistent data volume.

> **Why a Droplet and not App Platform?** MemeRadar keeps its trade history /
> equity curve in a SQLite file. DO **App Platform** has an *ephemeral*
> filesystem, so that history resets on every deploy/restart. A Droplet with a
> mounted volume keeps it. (An App Platform spec is included at
> [`.do/app.yaml`](.do/app.yaml) if you accept that trade-off.)

Cost: a **`basic` Droplet ($6/mo)** is plenty.

---

## Option A — Droplet + Docker Compose (recommended)

### 1. Create the Droplet
- DigitalOcean → **Create → Droplets**
- Image: **Ubuntu 24.04 LTS** (the **Docker** Marketplace image is even easier —
  Docker pre-installed)
- Plan: **Basic / Regular / $6/mo** (1 vCPU, 1 GB RAM)
- Authentication: **SSH key** (recommended)
- Create, then note the public IP.

### 2. Install Docker (skip if you used the Docker Marketplace image)
```bash
ssh root@YOUR_DROPLET_IP
curl -fsSL https://get.docker.com | sh
```

### 3. Get the code
```bash
git clone https://github.com/christianhillman15-afk/memecoin.git
cd memecoin
git checkout claude/memecoin-pump-dump-bot-jc07ac
```

### 4. Configure
```bash
cp .env.example .env
nano .env          # set DASHBOARD_PASSWORD (required for public access!)
                   # optionally set HELIUS_API_KEY for real whale data
```

### 5. Run it
```bash
docker compose up -d --build
docker compose logs -f      # watch it boot + scan; Ctrl-C to stop watching
```

By default the app binds to **loopback only** (`127.0.0.1:8000`) — i.e. **not**
reachable from the internet yet. Pick how you want to reach it:

#### 5a. Quick & private: SSH tunnel (no domain needed)
From your laptop:
```bash
ssh -L 8000:127.0.0.1:8000 root@YOUR_DROPLET_IP
```
Then open **http://localhost:8000**. The bot runs on the server; the tunnel just
forwards the dashboard to you. Nothing is exposed publicly.

#### 5b. Public HTTPS with a login (recommended for "check it from my phone")
1. Point a DNS **A-record** (e.g. `memeradar.yourdomain.com`) at the Droplet IP.
2. Edit [`Caddyfile`](Caddyfile): set your domain and a password hash:
   ```bash
   docker run --rm caddy caddy hash-password --plaintext 'your-strong-password'
   # paste the output into the Caddyfile in place of REPLACE_WITH_BCRYPT_HASH
   ```
3. Start with the proxy profile:
   ```bash
   docker compose --profile proxy up -d --build
   ```
   Caddy fetches a free Let's Encrypt certificate automatically and serves the
   dashboard at **https://memeradar.yourdomain.com** behind a login.

### 6. DigitalOcean Cloud Firewall (do this for any public exposure)
Networking → Firewalls → Create:
- **Inbound:** SSH (22) from your IP; HTTP (80) + HTTPS (443) from anywhere
  *(only if using the Caddy proxy)*.
- Do **not** open 8000 to the world — keep it loopback (5a/5b handle access).

### Updating to the latest code
```bash
cd memecoin
git pull
docker compose up -d --build          # add --profile proxy if you use Caddy
```

### Useful commands
```bash
docker compose ps                      # status + health
docker compose logs -f memeradar       # live logs
docker compose restart memeradar       # restart the bot
docker compose down                    # stop (data volume is preserved)
```

Your paper-trading history lives in the named Docker volume `memeradar-data`
and survives restarts, rebuilds, and `docker compose down`.

---

## Option B — DigitalOcean App Platform (no server to manage)

Trade-off: **trade history resets on every deploy** (ephemeral filesystem).

```bash
# install doctl + authenticate first: https://docs.digitalocean.com/reference/doctl/
doctl apps create --spec .do/app.yaml
```
Then set `DASHBOARD_PASSWORD` (and optionally `HELIUS_API_KEY`) as encrypted
env vars in the app settings. App Platform builds from the `Dockerfile` and
redeploys automatically on every push to the branch.

---

## Security checklist before going public
- [ ] `DASHBOARD_PASSWORD` is set to something strong (gates the **Reset** button).
- [ ] App stays on loopback **or** sits behind the Caddy proxy — never expose
      port 8000 directly without a firewall.
- [ ] DigitalOcean Cloud Firewall restricts SSH to your IP.
- [ ] Remember: this is **paper trading**. No keys with real funds are ever
      needed or used by MemeRadar.
