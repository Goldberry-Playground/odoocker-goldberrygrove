---
name: deploy-test
description: |
  DevOps deployment and testing skill for the Grove multi-tenant ecosystem.
  Use when deploying to DigitalOcean, testing locally with OrbStack, spinning up
  sandbox/QA environments, running docker compose stacks, debugging containers,
  managing domains/TLS, or when the user mentions "deploy", "test environment",
  "staging", "sandbox", "orbstack", "local test", "production", "CI/CD",
  "docker compose up", "ghost", "nginx", or "droplet".
version: 1.0.0
user-invocable: false
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, Agent
---

# Grove Deploy & Test Skill

## Overview

Unified DevOps skill for the Gather at the Grove ecosystem — covers local testing
with OrbStack, DigitalOcean droplet deployment, sandbox/QA environments, and CI/CD.

**Stack**: Odoo 19 + PostgreSQL 17 + Nginx + Ghost CMS (x3) + KeyDB + MinIO
**Tenants**: Goldberry Grove Farm, GGG Woodworking, At The Grove Nursery
**Infrastructure**: DigitalOcean Droplet (Docker Compose) + OrbStack (local dev)

---

## Quick Decision Tree

```
What do you need?
├── Local development/testing
│   ├── Quick test → OrbStack: Local Compose (Method 1)
│   ├── Full stack with Ghost → OrbStack: Grove Overlay (Method 2)
│   └── Sandbox/QA testing → OrbStack: Sandbox Overlay (Method 3)
│
├── Deploy to production
│   ├── First deploy → Production Setup Checklist
│   ├── Code update → SSH + git pull + compose restart
│   └── Config change → Update .env + compose up -d
│
├── CI/CD
│   ├── PR validation → GitHub Actions (ci.yml — already configured)
│   └── Auto-deploy → GitHub Actions Deploy Workflow
│
└── Debug/troubleshoot
    ├── Container won't start → Docker Logs Analysis
    ├── Domain/TLS issues → Nginx + Let's Encrypt Debug
    └── Database issues → Postgres Troubleshooting
```

---

## Environment Matrix

| Environment | Compose Files | .env Source | Domain |
|-------------|--------------|-------------|--------|
| **Local** | `base` + `override.local.yml` | `.env` (from `.env.example`) | `localhost:8069` |
| **Grove Local** | `base` + `override.grove.yml` + `override.local.yml` | `.env` + `.env.grove` | `localhost:8069` + Ghost on 2368-2370 |
| **Sandbox/QA** | `base` + `override.grove.yml` + `override.sandbox.yml` | `.env` + `.env.grove` (sandbox vars) | `erp-sandbox.goldberrygrove.farm` |
| **Production** | `base` + `override.grove.yml` + `override.production.yml` | `.env` + `.env.grove` | `erp.gatheringatthegrove.com` |

---

## Local Testing with OrbStack

### Why OrbStack

OrbStack replaces Docker Desktop on macOS with better performance, native
networking, and Kubernetes support. It's already the active Docker context
for this project.

| Feature | OrbStack | Docker Desktop |
|---------|----------|----------------|
| Memory usage | ~200 MB | ~2 GB |
| Startup time | < 2s | 30-60s |
| Linux VM access | `orb` shell | N/A |
| Docker context | `orbstack` (active) | `default` |
| Kubernetes | Built-in, optional | Built-in, heavier |
| File sharing | VirtioFS (fast) | gRPC-FUSE (slower) |

### Method 1: Basic Local Stack (Odoo + Postgres only)

```bash
# From project root
cp .env.example .env
# Edit .env — set APP_ENV=local, WORKERS=0, DEV_MODE=reload,xml

docker compose -f docker-compose.yml \
  -f docker-compose.override.local.yml \
  --profile odoo --profile postgres \
  up -d

# Access: http://localhost:8069
```

### Method 2: Full Grove Stack (with Ghost CMS)

```bash
cp .env.example .env
cp .env.grove.example .env.grove

# Edit .env.grove — for local testing, override Ghost URLs:
# GHOST_GOLDBERRY_URL=http://localhost:2368
# GHOST_GGG_URL=http://localhost:2369
# GHOST_NURSERY_URL=http://localhost:2370

docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.local.yml \
  --profile odoo --profile postgres --profile nginx \
  up -d

# Odoo: http://localhost:8069
# Ghost Goldberry: http://localhost:2368
# Ghost GGG: http://localhost:2369
# Ghost Nursery: http://localhost:2370
```

### Method 3: Sandbox/QA Environment

```bash
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.sandbox.yml \
  --profile odoo --profile postgres --profile nginx \
  up -d

# Uses APP_ENV=staging, DB=grove_sandbox, Ghost in development mode
# All containers have restart: "no" — failures are visible immediately
```

### OrbStack-Specific Tips

```bash
# Access the Linux VM directly
orb

# View container resource usage (lighter than docker stats)
orbctl status

# Reset Docker state without restarting
docker system prune -f

# OrbStack automatically handles:
# - Port forwarding (no extra config needed)
# - DNS resolution for containers
# - File mount performance (VirtioFS)
# - Resource limits (respects compose deploy.resources)
```

### Validating Local Environment

```bash
# 1. Check all containers are running
docker compose ps

# 2. Check Odoo is responding
curl -s -o /dev/null -w "%{http_code}" http://localhost:8069/web/login

# 3. Check Postgres connectivity
docker compose exec postgres pg_isready -U odoo

# 4. Check Ghost instances (if grove overlay active)
for port in 2368 2369 2370; do
  echo "Ghost :$port → $(curl -s -o /dev/null -w '%{http_code}' http://localhost:$port)"
done

# 5. Check nginx proxy chain (if nginx profiles active)
docker compose logs nginx --tail 20

# 6. Validate env var expansion
docker compose config --services
```

---

## DigitalOcean Production Deployment

### Architecture

```
                    ┌─────────────────────────────────────────┐
                    │         DigitalOcean Droplet             │
                    │         (4 GB RAM / 2 vCPU min)         │
                    │                                         │
  HTTPS ──────────► │  nginx-proxy (:80/:443)                 │
                    │    ├── acme-companion (Let's Encrypt)    │
                    │    │                                     │
                    │    ├── nginx (inner) ──► Odoo (:8069)    │
                    │    │     catch-all → proxy_pass odoo     │
                    │    │                                     │
                    │    └── Ghost routing (grove-ghost.conf)  │
                    │         ├── blog.goldberrygrove.farm     │
                    │         │     → ghost-goldberry (:2368)  │
                    │         ├── blog.woodworkingeorge.com    │
                    │         │     → ghost-ggg (:2369)        │
                    │         └── blog.atthegrovenursery.com   │
                    │               → ghost-nursery (:2370)    │
                    │                                         │
                    │  PostgreSQL 17                           │
                    │  KeyDB (Redis-compatible)                │
                    │  MinIO (S3-compatible)                   │
                    └─────────────────────────────────────────┘
```

### Domains & DNS

| Domain | Purpose | Points To |
|--------|---------|-----------|
| `erp.gatheringatthegrove.com` | Odoo backend/admin | Droplet IP |
| `goldberrygrove.farm` | Goldberry Grove website | Droplet IP |
| `woodworkingeorge.com` | GGG Woodworking website | Droplet IP |
| `atthegrovenursery.com` | At The Grove Nursery website | Droplet IP |
| `blog.goldberrygrove.farm` | Ghost CMS (Goldberry) | Droplet IP |
| `blog.woodworkingeorge.com` | Ghost CMS (GGG) | Droplet IP |
| `blog.atthegrovenursery.com` | Ghost CMS (Nursery) | Droplet IP |

All DNS A-records must point to the droplet's public IP. The nginx-proxy container
reads VIRTUAL_HOST and routes accordingly. acme-companion auto-obtains TLS certs
for every domain in LETSENCRYPT_HOST.

### First Deploy Checklist

```bash
# 1. Provision droplet (Ubuntu 24.04, Docker pre-installed)
# 2. SSH in and clone the repo
ssh root@<DROPLET_IP>
git clone <REPO_URL> /opt/grove && cd /opt/grove

# 3. Configure environment
cp .env.example .env
cp .env.grove.example .env.grove
# Edit both files with production values:
#   - Real domain names
#   - Strong passwords (ADMIN_PASSWD, DB_PASSWORD, etc.)
#   - WORKERS=4 (or 2*CPU+1)
#   - ACME_CA_URI → switch from staging to production Let's Encrypt
#   - APP_ENV=production

# 4. Switch Let's Encrypt to production
# In .env: ACME_CA_URI=https://acme-v02.api.letsencrypt.org/directory

# 5. Bring up the full stack
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.production.yml \
  --profile odoo --profile postgres --profile nginx --profile proxy --profile acme \
  up -d

# 6. Verify TLS certificates (may take 1-2 minutes)
curl -vI https://erp.gatheringatthegrove.com 2>&1 | grep -i "subject\|issuer"

# 7. Create Odoo database
# Visit https://erp.gatheringatthegrove.com/web/database/manager
```

### Updating Production

```bash
ssh root@<DROPLET_IP>
cd /opt/grove

# Pull latest code
git pull origin main

# Rebuild only if Dockerfile changed
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.production.yml \
  build odoo

# Restart with zero-downtime for config changes
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.production.yml \
  up -d --no-deps odoo nginx
```

---

## CI/CD with GitHub Actions

### Existing Pipeline (ci.yml)

Already configured to run on push/PR to `main`:
- **lint-python**: Ruff checks on `odoo/custom-addons/`
- **validate-compose**: YAML syntax validation for grove/sandbox overlays
- **validate-nginx**: Brace-balance check on `nginx/grove-ghost.conf`

### Deploy Workflow (add when ready)

```yaml
# .github/workflows/deploy.yml
name: Deploy to Production

on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production
    steps:
      - uses: actions/checkout@v4

      - name: Deploy to Droplet
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.DROPLET_IP }}
          username: ${{ secrets.DROPLET_USER }}
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          script: |
            cd /opt/grove
            git pull origin main
            docker compose -f docker-compose.yml \
              -f docker-compose.override.grove.yml \
              -f docker-compose.override.production.yml \
              build --pull odoo
            docker compose -f docker-compose.yml \
              -f docker-compose.override.grove.yml \
              -f docker-compose.override.production.yml \
              up -d
            # Wait for health
            sleep 10
            curl -sf http://localhost:8069/web/login || exit 1
```

### Required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `DROPLET_IP` | DigitalOcean droplet public IP |
| `DROPLET_USER` | SSH user (e.g., `root` or `deploy`) |
| `SSH_PRIVATE_KEY` | SSH private key for droplet access |

---

## Troubleshooting

### Container Won't Start

```bash
# Check logs for the failing container
docker compose logs <service> --tail 50

# Common Odoo issues:
# - "database does not exist" → Create DB via /web/database/manager
# - "connection refused" on postgres → Postgres not ready yet, restart odoo
# - "Address already in use" → Another process on the port
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
```

### TLS/Certificate Issues

```bash
# Check acme-companion logs
docker compose logs letsencrypt --tail 30

# Common issues:
# - Still using staging CA → ACME_CA_URI must be production URL
# - DNS not propagated → nslookup <domain> should return droplet IP
# - Rate limited → Wait 1 hour, check https://letsencrypt.org/docs/rate-limits/

# Force cert renewal
docker compose exec letsencrypt /app/force_renew
```

### Postgres Issues

```bash
# Check postgres is running and accepting connections
docker compose exec postgres pg_isready -U odoo

# Check postgres logs for errors
docker compose logs postgres --tail 30

# Connect to postgres directly
docker compose exec postgres psql -U odoo -d postgres

# Check shared memory (if OOM or crashes)
docker compose exec postgres cat /proc/meminfo | grep Shmem
```

### Ghost CMS Issues

```bash
# Check individual Ghost instance
docker compose logs ghost-goldberry --tail 20

# Common issues:
# - "url" env var mismatch → Must match the public URL exactly
# - SQLite lock → Only one Ghost per volume, check mounts
# - Port conflict → Each Ghost must use a unique port (2368/2369/2370)
```

### OrbStack-Specific Issues

```bash
# Reset Docker state
orbctl stop && orbctl start

# Check OrbStack resource usage
orbctl status

# If containers are slow, check VirtioFS mounts
docker inspect <container> | jq '.[0].Mounts'

# Force rebuild from scratch (nuclear option)
docker compose down -v
docker system prune -af
docker compose up -d --build
```

---

## Best Practices

### Environment Isolation

1. **Never share `.env` files** between environments — each gets its own copy
2. **Use profiles** to control which services start: `--profile odoo --profile postgres`
3. **Sandbox uses `restart: "no"`** so failures surface immediately instead of restart-looping
4. **Production uses `restart: unless-stopped`** for auto-recovery

### Security

1. **Change default passwords** before first production deploy (ADMIN_PASSWD, DB_PASSWORD)
2. **Bind ports to 127.0.0.1** in production (done in `override.production.yml`)
3. **Switch ACME_CA_URI** from staging to production before go-live
4. **Set LIST_DB=False and DBFILTER** in production to prevent database enumeration
5. **Use SSH keys** for droplet access, disable password auth

### Performance

1. **PostgreSQL tuning** is in `.env.example` — calibrated for 4 GB RAM droplet
2. **Odoo workers** should be `2 * CPU_CORES + 1` in production (WORKERS=5 for 2 vCPU)
3. **Memory limits** in `override.production.yml` prevent OOM kills
4. **Ghost uses SQLite** — no additional database overhead per instance
5. **KeyDB** is Redis-compatible but more memory-efficient

### Compose File Layering

```bash
# Always layer files in this order:
# 1. Base:       docker-compose.yml           (service definitions)
# 2. Ecosystem:  docker-compose.override.grove.yml   (Ghost services)
# 3. Environment: docker-compose.override.<env>.yml  (ports, resources, restart)

# Local development
docker compose -f docker-compose.yml -f docker-compose.override.local.yml up

# Grove local
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.local.yml up

# Production
docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.production.yml up -d
```

---

## Integration with DigitalOcean Services

### Managed Database (Future Migration Path)

When traffic outgrows the self-hosted PostgreSQL:

1. Create DO Managed PostgreSQL: `doctl databases create grove-db --engine pg --version 17 --region nyc1 --size db-s-2vcpu-4gb`
2. Update `.env`: Set `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_SSLMODE=require` to managed DB values
3. Remove postgres from compose profiles
4. Add managed DB firewall: `doctl databases firewalls append <db-id> --rule ip_addr:<droplet-ip>`

### Spaces / S3 (Already Configured)

MinIO is the local S3-compatible store. For production with DO Spaces:
1. Create Space: `doctl compute cdn create --origin <space-name>.nyc3.digitaloceanspaces.com`
2. Update `.env`: Set `AWS_HOST`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
3. Remove MinIO from compose profiles

### Load Balancer (Future Scaling)

For multi-droplet setup:
1. Create LB: `doctl compute load-balancer create --name grove-lb --region nyc1`
2. Point DNS to LB instead of droplet IP
3. Each droplet runs the same compose stack
4. Session affinity via Redis (already configured with KeyDB)
