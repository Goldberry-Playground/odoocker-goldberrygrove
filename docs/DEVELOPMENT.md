# Local Development Guide

## Prerequisites

- **OrbStack** (Docker runtime): `brew install orbstack`
- **Git**: access to `Goldberry-Playground` org
- **Claude Code** (optional): for AI-assisted development with Odoo MCP

## Initial Setup

```bash
# 1. Clone both repos side by side
cd ~/Documents/Dev\ Projects/gather-at-the-grove/
git clone git@github.com:Goldberry-Playground/odoocker-goldberrygrove.git odoocker
git clone git@github.com:Goldberry-Playground/grove-odoo-modules.git

# 2. Configure environment
cd odoocker
cp .env.example .env
# Edit .env: set APP_ENV=fresh for first run (creates DB via web UI)

# 3. Build and start
docker compose -f docker-compose.yml -f docker-compose.override.local.yml \
  --profile odoo --profile postgres build
docker compose -f docker-compose.yml -f docker-compose.override.local.yml \
  --profile odoo --profile postgres up -d

# 4. Create database
# Visit http://localhost:8069/web/database/manager
# Master password is the ADMIN_PASSWD value from .env (default: odoo)

# 5. Switch to local mode for ongoing development
# Edit .env: APP_ENV=local, DEV_MODE=reload,xml
# Rebuild: docker compose ... build odoo && docker compose ... up -d
```

## Daily Workflow

```bash
# Start the stack
docker compose -f docker-compose.yml -f docker-compose.override.local.yml \
  --profile odoo --profile postgres up -d

# View logs
docker compose -f docker-compose.yml -f docker-compose.override.local.yml logs -f odoo

# Stop
docker compose -f docker-compose.yml -f docker-compose.override.local.yml down
```

## Module Development

Custom modules live in `../grove-odoo-modules/`, which is bind-mounted into the Odoo container at `/workspace/current`.

1. Edit module files in `grove-odoo-modules/`
2. Changes to Python files require an Odoo restart (or use `DEV_MODE=reload`)
3. Changes to XML views require module upgrade:
   ```bash
   docker compose ... exec odoo odoo -u grove_headless --stop-after-init
   ```
4. To install a new module: Odoo > Apps > Update Apps List > search and install

## MCP Server (AI-Assisted Development)

```bash
# Install (one-time)
pipx install odoo-mcp-multi

# Configure profile
odoo-mcp edit-profile --name dev --password <your-admin-password>

# Test connection
odoo-mcp test --profile dev

# Use in Claude Code — .mcp.json auto-configures the server
```

## Full Grove Stack (with Ghost CMS)

```bash
cp .env.grove.example .env.grove

docker compose -f docker-compose.yml \
  -f docker-compose.override.grove.yml \
  -f docker-compose.override.local.yml \
  --profile odoo --profile postgres --profile nginx up -d

# Ghost instances:
# Goldberry: http://localhost:2368
# GGG:       http://localhost:2369
# Nursery:   http://localhost:2370
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Odoo won't start | Check `docker compose logs odoo --tail 30` |
| "database does not exist" | Create via `/web/database/manager` |
| Module not found | Restart Odoo, check addons_path in logs |
| Port conflict | `lsof -i :8069` to find conflicting process |
