#!/bin/bash

set -e

# Ensure the git-sync workspace path exists so Odoo can include it in addons_path
# even when the custom-modules-sync container is not running.
# When git-sync IS running it replaces /workspace/current with a symlink to the
# latest module checkout — Odoo follows symlinks in addons_path correctly.
mkdir -p /workspace/current 2>/dev/null || true

# Generate odoo.conf from /odoo.conf template + /.env (bind-mounted at
# runtime). Previously /odoorc.sh ran at BUILD time, which required
# baking /.env into the image. Moving it here keeps secrets out of
# every image layer; the trade-off is a 1-2s startup cost. The script
# runs as the `odoo` user (image USER), so generated files inherit
# the right ownership without an explicit chown.
if [ -x /odoorc.sh ] && [ -f /.env ]; then
    cd / && /odoorc.sh
fi

# Set HOST/PORT/USER/PASSWORD defaults that wait-for-psql.py expects below.
# This script's wait-for-psql.py invocations later interpolate ${HOST} etc.,
# which would be empty (and argparse fails with "expected one argument") if
# we don't set them. Cascade matches the upstream odoo:19 entrypoint's
# defaults but uses our canonical compose-env names (DB_HOST/DB_PORT/DB_USER
# /DB_PASSWORD set via docker-compose `environment:` block) on top of the
# legacy docker-link names (DB_PORT_5432_TCP_*) the upstream image relied on.
# The :=' syntax means "set HOST if unset OR empty"; the resolved value
# becomes the bash env var that the rest of this script references.
: ${HOST:=${DB_HOST:=${DB_PORT_5432_TCP_ADDR:='db'}}}
: ${PORT:=${DB_PORT:=${DB_PORT_5432_TCP_PORT:=5432}}}
: ${USER:=${DB_USER:=${DB_ENV_POSTGRES_USER:=${POSTGRES_USER:='odoo'}}}}
: ${PASSWORD:=${DB_PASSWORD:=${DB_ENV_POSTGRES_PASSWORD:=${POSTGRES_PASSWORD:='odoo'}}}}

# Hash the admin_passwd in odoo.conf so Odoo 19 accepts it.
# The .env → odoorc.sh pipeline writes plaintext, but Odoo 19 requires
# pbkdf2-hashed passwords for the database manager.
if command -v python3 &>/dev/null && [ -f "${ODOO_RC:-/usr/lib/python3/dist-packages/odoo/odoo.conf}" ]; then
    python3 -c "
import re, sys
from pathlib import Path
try:
    from passlib.context import CryptContext
    conf = Path('${ODOO_RC:-/usr/lib/python3/dist-packages/odoo/odoo.conf}')
    text = conf.read_text()
    m = re.search(r'^admin_passwd\s*=\s*(.+)$', text, re.MULTILINE)
    if m:
        val = m.group(1).strip()
        if not val.startswith('\$pbkdf2'):
            ctx = CryptContext(schemes=['pbkdf2_sha512'])
            hashed = ctx.hash(val)
            text = text[:m.start(1)] + hashed + text[m.end(1):]
            conf.write_text(text)
            print(f'Hashed admin_passwd in odoo.conf')
except Exception as e:
    print(f'WARN: could not hash admin_passwd: {e}', file=sys.stderr)
" 2>&1 || true
fi

# Safe .env parser — replaces the previous `eval "$key=\"$value\""` loop.
# eval would execute `$(cmd)` or backticks embedded in any .env value at
# container startup. Operators control .env, but defense-in-depth says:
# never pipe untrusted-shaped data through eval in the startup path.
#
# This parser only expands `${VAR}` references against values seen earlier
# in the same .env (or pre-existing environment vars). It deliberately does
# NOT expand `$VAR` (no braces), `$(cmd)`, backticks, or arithmetic `$(())`
# — those stay as literal characters in the resulting value.
expand_env_refs() {
    local input="$1"
    local output=""
    local rest="$input"
    while [[ "$rest" == *'${'*'}'* ]]; do
        local prefix="${rest%%\$\{*}"
        local after="${rest#*\$\{}"
        local name="${after%%\}*}"
        local tail="${after#*\}}"
        if [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
            output+="${prefix}${!name}"
        else
            output+="${prefix}\${${name}}"
        fi
        rest="$tail"
    done
    output+="$rest"
    printf '%s' "$output"
}

# Load /.env into bash env IF it exists. The QA env bind-mounts
# /etc/grove/.env -> /.env, but a plain `docker run grove-odoo:latest` (no
# bind mount) doesn't have /.env -- the previous unguarded `done < .env`
# crashed under `set -e` with ".env: No such file or directory", which
# turned any docker run (preflights, smoke tests, ad-hoc image inspection)
# into a hard failure. Now if /.env is absent, we skip the loader entirely
# and trust the container env (compose's `environment:` block, `docker run
# -e`, etc.) to provide the values the substitution + APP_ENV branches need.
if [ -f .env ]; then
    while IFS='=' read -r key value || [[ -n $key ]]; do
        # Skip comments and empty lines
        [[ $key =~ ^[[:space:]]*# ]] && continue
        [[ -z ${key// /} ]] && continue
        # Trim surrounding whitespace from key
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        # Validate key is a legal identifier; otherwise skip the line
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # Strip a single layer of surrounding double-quotes
        if [[ ${#value} -ge 2 && "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
            value="${value:1:${#value}-2}"
        fi
        # Expand ${VAR} refs against vars set so far — NEVER via eval.
        value="$(expand_env_refs "$value")"
        # Assign without eval. printf -v assigns by indirect name.
        printf -v "$key" '%s' "$value"
    done < .env
fi

# Check the USE_REDIS to add base_attachment_object_storage & session_redis to LOAD variable
if [[ $USE_REDIS == "true" ]]; then
    LOAD+=",session_redis"
fi

# Check the USE_REDIS to add attachment_s3 to LOAD variable
if [[ $USE_S3 == "true" ]]; then
    LOAD+=",base_attachment_object_storage"
    LOAD+=",attachment_s3"
fi

# Check the USE_REDIS to add sentry to LOAD variable
if [[ $USE_SENTRY == "true" ]]; then
    LOAD+=",sentry"
fi

# ── GOL-1859: seed web.base.url + freeze (reproducible, env-driven) ──────────
# Odoo generates every ABSOLUTE URL it emits off the `web.base.url`
# ir.config_parameter: password-reset / set-password links, sale-order
# confirmation + customer-portal links, e-commerce / website links, and (via
# report.url) PDF report asset URLs. Its out-of-the-box default is
# http://localhost:8069, and — unless `web.base.url.freeze` is 'True' — Odoo
# silently REWRITES it to whatever Host the next authenticated web request
# arrives on. On a headless prod box sitting behind Caddy, that drifts to
# localhost:8069, so every link delivered to a customer is unusable by any
# external party (the GOL-1859 defect). This also defeats the transactional-
# email SMTP work: wiring Mailgun does not help if every link points at
# localhost.
#
# This seed makes the correct value REPRODUCIBLE instead of a one-off live
# mutation that the next immutable rebuild (GOL-920: prod root disk is
# destroyed on replace) silently reverts. It runs on every deploy-shaped boot
# (the production / qa / staging branches call seed_web_base_url just before
# their serve exec) and upserts both params, so a droplet rebuild OR a
# from-scratch bootstrap converges to the right host. It is driven by the
# WEB_BASE_URL env var (compose `environment:` block <- /etc/grove/.env <- TF),
# so changing the value is CONFIG, never a code edit; envs that do not set
# WEB_BASE_URL (local / preview / testing) are a no-op. freeze='True' then stops
# Odoo ever rewriting it again — belt (re-seed each boot) and suspenders
# (freeze).
#
# IDEMPOTENT: set_param writes the same value on every boot (no-op when
# unchanged). FAILS SOFT: on a brand-new EMPTY DB (base not yet installed) the
# shell cannot load a registry; we log a WARN and let the serve command's
# --init=base bootstrap the DB, then the NEXT boot seeds it. Prod's Managed-PG
# DB is durable (survives the immutable replace) and already initialised, so on
# prod the seed lands on the very first boot after this ships.
seed_web_base_url() {
    local db="$1"
    if [ -z "${WEB_BASE_URL:-}" ]; then
        return 0
    fi
    echo "GOL-1859: seeding web.base.url=${WEB_BASE_URL} (+ freeze, report.url) on database '${db}'"
    # Pass the value through the subprocess env (never string-interpolated into
    # the Python heredoc) so an odd character in the URL can never inject code.
    WEB_BASE_URL="${WEB_BASE_URL}" odoo shell \
        --config ${ODOO_RC} --database="${db}" --no-http --log-level=warn <<'PY' || \
        echo "WARN(GOL-1859): web.base.url seed skipped (DB not ready / base not installed?); it will retry on the next boot"
import os
url = (os.environ.get("WEB_BASE_URL") or "").strip()
if url:
    icp = env["ir.config_parameter"].sudo()
    icp.set_param("web.base.url", url)
    icp.set_param("web.base.url.freeze", "True")
    icp.set_param("report.url", url)
    env.cr.commit()
    print("GOL-1859: set web.base.url=%s web.base.url.freeze=True report.url=%s" % (url, url))
PY
}

case "$1" in
    -- | odoo)
        shift
        if [[ "$1" == "scaffold" ]] ; then
            # Creates new module.
            exec odoo "$@"
        else
            wait-for-psql.py --db_host ${HOST} --db_port ${PORT} --db_user ${USER} --db_password ${PASSWORD} --timeout=30

            # GOL-1009: revision-gated custom-module upgrade at boot.
            #
            # WHY: git-sync (custom-modules-sync sidecar) delivers new module
            # CODE into /workspace/current, but the qa + production branches
            # below boot with --init=base and NO --update -- deliberately, so a
            # plain `restart: unless-stopped` bounce doesn't re-run migrations
            # (see the qa/production branch comments + GOL-746). The gap: a
            # module's NEW models (added since the last upgrade) load into
            # Odoo's Python registry on the next boot WITHOUT their DB tables
            # ever being created, because only an explicit `-u <module>` runs a
            # model's DDL. Result: psycopg2 UndefinedTable 500 the first time
            # the new model is touched (GOL-985 shipped grove.publish.event;
            # Publish -> `relation "grove_publish_event" does not exist`).
            #
            # FIX: when the git-synced revision advances, run ONE blocking
            # `--init=base,<mods> --update=<mods> --stop-after-init` pass before
            # the server starts, so new DDL always lands on deploy. Opt-in via
            # AUTO_UPGRADE_MODULES (comma list) -- set ONLY in the qa +
            # production compose environment blocks, so local/preview/testing
            # are untouched. Including <mods> in --init makes a from-scratch
            # env self-serve (installs the module -> full DDL) instead of the
            # old undocumented manual `-i` step, and guarantees --update never
            # targets a not-installed module.
            #
            # IDEMPOTENT: the last-upgraded revision is recorded in a marker on
            # the durable filestore volume (/var/lib/odoo, survives a droplet
            # replace). A restart on the SAME revision is a no-op (preserves the
            # "no --update on every restart" design); only a revision advance
            # (a real code change, or a deliberate git-sync SHA-pin bump on
            # prod per GOL-987) re-runs the upgrade. The revision is git-sync's
            # per-commit worktree name -- basename of the /workspace/current
            # symlink target (git-sync v4 repoints it to a new SHA-named
            # worktree on every synced commit).
            #
            # FAILS LOUD: under `set -e` a failed upgrade aborts boot ->
            # restart loop, logged in `docker logs` -- rather than serving a
            # half-migrated DB. The marker is written ONLY after a successful
            # upgrade, so a failed migration retries on the next boot.
            if [ -n "${AUTO_UPGRADE_MODULES:-}" ]; then
                _gitsync_target="$(readlink -f /workspace/current 2>/dev/null || true)"
                _synced_rev=""
                [ -n "$_gitsync_target" ] && _synced_rev="$(basename "$_gitsync_target")"
                _rev_marker="${MODULE_UPGRADE_MARKER:-/var/lib/odoo/.grove-modules-rev}"
                _last_rev="$(cat "$_rev_marker" 2>/dev/null || true)"
                if [ -n "$_synced_rev" ] && [ "$_synced_rev" != "$_last_rev" ]; then
                    echo "GOL-1009: git-synced modules revision advanced ('${_last_rev:-<none>}' -> '${_synced_rev}'); running --init=base,${AUTO_UPGRADE_MODULES} --update=${AUTO_UPGRADE_MODULES} --stop-after-init on ${DB_NAME:-<unset>}"
                    odoo --config ${ODOO_RC} --database=${DB_NAME} --init=base,${AUTO_UPGRADE_MODULES} --update=${AUTO_UPGRADE_MODULES} --stop-after-init --no-http --workers=0 --without-demo=all --load-language=
                    printf '%s\n' "$_synced_rev" > "$_rev_marker"
                    echo "GOL-1009: module upgrade complete; recorded revision '${_synced_rev}' in ${_rev_marker}"
                else
                    echo "GOL-1009: modules revision unchanged ('${_synced_rev:-<none>}'); skipping upgrade (plain restart, no --update)"
                fi
            fi

            if [ ${APP_ENV} = 'fresh' ] || [ ${APP_ENV} = 'restore' ] || [ ${APP_ENV} = 'preview' ]; then
                # Ideal for a fresh install or restore a production database.
                # APP_ENV=preview (per-PR ephemeral droplets) is a restore case:
                # cloud-init restore.sh loads a sanitized grove_preview snapshot
                # BEFORE odoo boots, so odoo must just serve the existing DB with
                # no --init/--update. Without this branch APP_ENV=preview matched
                # no case and the container exec'd nothing → odoo silently down.
                echo odoo --config ${ODOO_RC} --database= --init= --update= --load=${LOAD} --log-level=${LOG_LEVEL} --load-language= --workers=0 --limit-time-cpu=3600 --limit-time-real=7200

                exec odoo --config ${ODOO_RC} --database= --init= --update= --load-language= --workers=0 --limit-time-cpu=3600 --limit-time-real=7200
            fi

            if [ ${APP_ENV} = 'local' ] ; then
                # Listens to all .env variables mapped into odoo.conf file.
                echo odoo --config ${ODOO_RC} --database=${DB_NAME} --init=${INIT} --update=${UPDATE} --load=${LOAD} --workers=${WORKERS} --log-level=${LOG_LEVEL} --dev=${DEV_MODE}

                exec odoo --config ${ODOO_RC} --init=${INIT} --update=${UPDATE} --dev=${DEV_MODE}
            fi

            if [ ${APP_ENV} = 'debug' ] ; then
                # Same as local but you can debug you custom addons with your code editor (VSCode).
                echo debugpy odoo --config ${ODOO_RC}

                exec /usr/bin/python3 -m debugpy --listen ${DEBUG_INTERFACE}:${DEBUG_PORT} ${DEBUG_PATH} --config ${ODOO_RC}
            fi

            if [ ${APP_ENV} = 'testing' ] ; then
                # Initializies a fresh 'test_*' database, installs the addons to test, and runs tests you specify in the test tags.
                echo odoo --config ${ODOO_RC} --database=test_${DB_NAME} --test-enable --test-tags ${TEST_TAGS} --init=${ADDONS_TO_TEST} --update=${ADDONS_TO_TEST} --load=${LOAD} --log-level=${LOG_LEVEL} --without-demo= --workers=0 --dev= --stop-after-init

                exec odoo --config ${ODOO_RC} --database=test_${DB_NAME} --test-enable --test-tags ${TEST_TAGS} --init=${ADDONS_TO_TEST} --update=${ADDONS_TO_TEST} --without-demo= --workers=0 --dev= --stop-after-init
            fi

            if [ ${APP_ENV} = 'staging' ] ; then
                # Automagically upgrade all addons and install new ones. Ideal for deployment process.
                echo odoo --config ${ODOO_RC} --database=${DB_NAME} --init=${INIT} --update=all --load=${LOAD} --log-level=${LOG_LEVEL} --load-language=${LOAD_LANGUAGE} --limit-time-cpu=3600 --limit-time-real=7200 --dev=

                # GOL-1859: reproducible web.base.url seed (no-op unless WEB_BASE_URL set).
                seed_web_base_url "${DB_NAME}"

                exec odoo --config ${ODOO_RC} --database=${DB_NAME} --init=${INIT} --update=all --without-demo=all --workers=0 --limit-time-cpu=3600 --limit-time-real=7200 --dev=
            fi

            if [ ${APP_ENV} = 'qa' ] ; then
                # QA: like staging but no --update=all (don't re-update modules
                # on every container restart -- QA testers expect data + module
                # state to persist within a single droplet's lifetime). Demo
                # data also OFF since QA testers seed real-shaped data.
                # --init=base on an empty DB triggers Odoo's create-DB-and-
                # install-modules path, which is what we want on first boot
                # of a fresh QA droplet. On subsequent restarts (DB already
                # exists), --init=base is a no-op for the already-installed
                # base module.
                echo odoo --config ${ODOO_RC} --database=${DB_NAME:-grove_qa} --init=${INIT:-base} --load=${LOAD:-web} --workers=${WORKERS:-2} --log-level=${LOG_LEVEL:-info}

                # GOL-1859: same reproducible seed on QA. No-op unless the QA
                # compose sets WEB_BASE_URL (e.g. https://qa.gatheringatthegrove.com).
                seed_web_base_url "${DB_NAME:-grove_qa}"

                exec odoo --config ${ODOO_RC} --database=${DB_NAME:-grove_qa} --init=${INIT:-base} --without-demo=all --workers=${WORKERS:-2}
            fi

            if [ ${APP_ENV} = 'production' ] ; then
                # Level-3 production (ADR-007 Phase 6, GOL-105): a single fixed
                # database on DO Managed Postgres (DB_NAME), the same single-DB
                # shape validated under APP_ENV=qa. The legacy empty-`--database=`
                # form here assumed the old multi-DB/dbfilter model and never
                # targeted the fixed L3 DB -- it could not bootstrap a fresh
                # Managed-PG database. Now: --init=base on an EMPTY DB triggers
                # Odoo's create-DB-and-install-base path on first boot of a fresh
                # droplet; on later restarts (DB already populated) it is a no-op.
                # Deliberately NO --update=all on restart: prod module upgrades
                # are a deliberate deploy action, not a side effect of every
                # `restart: unless-stopped` bounce (which would risk downtime +
                # partial migrations). Demo data OFF.
                echo odoo --config ${ODOO_RC} --database=${DB_NAME:-grove_prod} --init=${INIT:-base} --load=${LOAD:-web} --workers=${WORKERS:-2} --log-level=${LOG_LEVEL:-info} --without-demo=all

                # GOL-1859: converge web.base.url + freeze before serving so every
                # emitted link (password reset, portal, e-commerce, reports) points
                # at the real prod host, not localhost:8069.
                seed_web_base_url "${DB_NAME:-grove_prod}"

                exec odoo --config ${ODOO_RC} --database=${DB_NAME:-grove_prod} --init=${INIT:-base} --without-demo=all --workers=${WORKERS:-2}
            fi
        fi
        ;;
    -*)

        wait-for-psql.py --db_host ${HOST} --db_port ${PORT} --db_user ${USER} --db_password ${PASSWORD} --timeout=30
        echo odoo --config ${ODOO_RC}
        exec odoo --config ${ODOO_RC}
        ;;
    *)

        echo "$@"
        exec "$@"
esac

exit 1
