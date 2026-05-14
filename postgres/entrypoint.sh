#!/bin/bash

set -e

# Source environment variables. The .env file is bind-mounted at /.env
# by docker-compose (NOT baked into the image — see Dockerfile comment).
source /.env

# Sanity-check the values we're about to interpolate into SQL. psql `:'var'`
# and `:"var"` substitution escapes special chars correctly, but the
# parameters still must be set — empty values would create an unnamed user
# or unquoted identifier with surprising semantics.
: "${POSTGRES_PORT:?required for postgres init}"
: "${POSTGRES_MAIN_USER:?required for postgres init}"
: "${POSTGRES_DB:?required for postgres init}"
: "${DB_TEMPLATE:?required for postgres init}"
: "${DB_USER:?required for postgres init}"
: "${DB_PASSWORD:?required for postgres init}"

# All psql commands below use `-v var=value` for parameter binding and
# `:"var"` (quoted identifier) / `:'var'` (quoted string) substitution.
# This stops SQL injection via DB_USER / DB_PASSWORD / DB_TEMPLATE values
# that contain single quotes or backslashes. Previously these were
# interpolated unquoted into psql -c "...$VAR..." strings.

PSQL_ADMIN="psql -p ${POSTGRES_PORT} -U ${POSTGRES_MAIN_USER}"

# Create the $DB_TEMPLATE database and install unaccent.
${PSQL_ADMIN} -d "${POSTGRES_DB}" -v t="${DB_TEMPLATE}" -c 'CREATE DATABASE :"t" WITH TEMPLATE = template0;'
${PSQL_ADMIN} -d "${DB_TEMPLATE}" -c "CREATE EXTENSION IF NOT EXISTS unaccent;"
${PSQL_ADMIN} -d "${DB_TEMPLATE}" -c "ALTER FUNCTION unaccent(text) IMMUTABLE;"

# Create Odoo user with the configured password and grant CREATEDB.
${PSQL_ADMIN} -d "${POSTGRES_DB}" \
    -v u="${DB_USER}" -v p="${DB_PASSWORD}" \
    -c 'CREATE USER :"u" WITH PASSWORD :'\''p'\'';'
${PSQL_ADMIN} -d "${POSTGRES_DB}" -v u="${DB_USER}" -c 'ALTER USER :"u" CREATEDB;'

# Grant template access to the Odoo user.
${PSQL_ADMIN} -d "${POSTGRES_DB}" -v t="${DB_TEMPLATE}" -v u="${DB_USER}" \
    -c 'GRANT ALL PRIVILEGES ON DATABASE :"t" TO :"u";'
${PSQL_ADMIN} -d "${DB_TEMPLATE}" -v t="${DB_TEMPLATE}" -v u="${DB_USER}" \
    -c 'ALTER DATABASE :"t" OWNER TO :"u";'

# Optional: PgAdmin's own metadata database. NOTE the upstream typo
# `PGADMING_*` (with a stray G) is preserved here because the rest of the
# codebase (.env.example, compose) also uses it — fixing the typo would
# be a coordinated rename across all files, out of scope for this PR.
if [[ "${USE_PGADMIN}" == "true" ]]; then
    : "${PGADMING_DB_NAME:?required when USE_PGADMIN=true}"
    : "${PGADMING_DB_USER:?required when USE_PGADMIN=true}"
    : "${PGADMIN_DB_PASSWORD:?required when USE_PGADMIN=true}"

    ${PSQL_ADMIN} -d "${POSTGRES_DB}" -v n="${PGADMING_DB_NAME}" \
        -c 'CREATE DATABASE :"n";'
    ${PSQL_ADMIN} -d "${POSTGRES_DB}" \
        -v u="${PGADMING_DB_USER}" -v p="${PGADMIN_DB_PASSWORD}" \
        -c 'CREATE USER :"u" WITH PASSWORD :'\''p'\'';'
    ${PSQL_ADMIN} -d "${POSTGRES_DB}" \
        -v n="${PGADMING_DB_NAME}" -v u="${PGADMING_DB_USER}" \
        -c 'GRANT ALL PRIVILEGES ON DATABASE :"n" TO :"u";'
    ${PSQL_ADMIN} -d "${PGADMING_DB_NAME}" \
        -v u="${PGADMING_DB_USER}" \
        -c 'GRANT ALL PRIVILEGES ON SCHEMA public TO :"u";'
    # Revoke Odoo user's access to pgadmin database.
    ${PSQL_ADMIN} -d "${POSTGRES_DB}" \
        -v n="${PGADMING_DB_NAME}" -v u="${DB_USER}" \
        -c 'REVOKE CONNECT ON DATABASE :"n" FROM :"u";'
fi

echo "Setup completed."
