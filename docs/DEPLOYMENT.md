# Production Deployment Checklist

## Prerequisites

- DigitalOcean droplet (8GB RAM / 4 vCPU recommended)
- Ubuntu 24.04 with Docker pre-installed
- DNS A-records pointing to droplet IP for all domains
- GitHub access for webhook configuration

## First Deploy

### 1. Server Setup

```bash
ssh root@<DROPLET_IP>

# Clone the repo
git clone git@github.com:Goldberry-Playground/odoocker-goldberrygrove.git /opt/grove
cd /opt/grove

# Configure environment
cp .env.example .env
cp .env.grove.example .env.grove
```

### 2. Edit `.env` — Critical Settings

```bash
APP_ENV=production
ADMIN_PASSWD=<strong-random-password>
DB_PASSWORD=<strong-random-password>
DB_NAME=gatheratthegrove
WORKERS=5                    # 2 * CPU_CORES + 1
LIST_DB=False
DBFILTER=gatheratthegrove
ACME_CA_URI=https://acme-v02.api.letsencrypt.org/directory   # PRODUCTION Let's Encrypt
```

### 3. Edit `.env.grove` — Ghost and Module Sync

```bash
# Real domain URLs for Ghost
GHOST_GOLDBERRY_URL=https://blog.goldberrygrove.farm
GHOST_GGG_URL=https://blog.woodworkingeorge.com
GHOST_NURSERY_URL=https://blog.atthegrovenursery.com

# Git-sync for custom modules
USE_CUSTOM_MODULES_SYNC=true
CUSTOM_MODULES_REPO=https://github.com/Goldberry-Playground/grove-odoo-modules.git
```

### 4. Start the Stack

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.production.yml \
  --profile odoo --profile postgres --profile nginx \
  --profile proxy --profile acme --profile git-sync \
  up -d
```

### 5. Verify

```bash
# Check all containers are running
docker compose ps

# Verify TLS (may take 1-2 minutes for cert issuance)
curl -vI https://erp.gatheringatthegrove.com 2>&1 | grep "subject\|issuer"

# Check Odoo health
curl -sf https://erp.gatheringatthegrove.com/web/login

# Verify git-sync is pulling modules
docker compose logs custom-modules-sync --tail 10
```

### 6. Create Database

Visit `https://erp.gatheringatthegrove.com/web/database/manager` and create the production database.

## DNS Records

| Domain | Type | Value |
|--------|------|-------|
| `erp.gatheringatthegrove.com` | A | `<DROPLET_IP>` |
| `goldberrygrove.farm` | A | `<DROPLET_IP>` |
| `woodworkingeorge.com` | A | `<DROPLET_IP>` |
| `atthegrovenursery.com` | A | `<DROPLET_IP>` |
| `blog.goldberrygrove.farm` | A | `<DROPLET_IP>` |
| `blog.woodworkingeorge.com` | A | `<DROPLET_IP>` |
| `blog.atthegrovenursery.com` | A | `<DROPLET_IP>` |

## Updating Production

```bash
ssh root@<DROPLET_IP>
cd /opt/grove
git pull origin main

# If Dockerfile changed:
docker compose ... build --pull odoo

# Restart
docker compose ... up -d --no-deps odoo nginx
```

## Module Updates (No Rebuild Needed)

Push to `grove-odoo-modules` repo → git-sync auto-pulls within 30 seconds.

For immediate sync, configure GitHub webhook:
- **URL**: `https://erp.gatheringatthegrove.com/modules-sync/`
- **Content type**: `application/json`
- **Events**: Push only

## Rollback

```bash
# Rollback infrastructure
git log --oneline -5          # Find the commit to revert to
git checkout <commit-hash>
docker compose ... build odoo && docker compose ... up -d

# Rollback modules
cd /tmp && git clone grove-odoo-modules
git log --oneline -5
git revert HEAD               # Or git checkout <hash>
git push origin main          # git-sync picks up the revert
```
