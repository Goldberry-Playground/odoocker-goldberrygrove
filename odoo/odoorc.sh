#!/bin/bash

set -e

# Define the path to the example configuration file
TEMPLATE_CONF="odoo.conf"

# First pass: Evaluate any nested variables within .env file and export them
while IFS='=' read -r key value || [[ -n $key ]]; do
    # Skip comments and empty lines
    [[ $key =~ ^#.* ]] || [[ -z $key ]] && continue
    
    # Removing any quotes around the value
    value=${value%\"}
    value=${value#\"}
    
    # Evaluate any variables within value
    eval "value=\"$value\""
    
    export "$key=$value"
done < .env

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

# Generate the substituted conf in a writable temp location, then
# truncate-and-write the final destination. This matters at runtime:
# odoorc.sh now runs as the `odoo` user (it used to run as root at
# build time), and $ODOO_RC typically lives in /usr/lib/python3/dist-
# packages/odoo/ which is root-owned. `sed -i` writes its temp file
# into the SAME directory as the target — that fails with "Permission
# denied" when the parent dir isn't writable, even if the target file
# itself is owned by us. Writing to $TEMP_RC (in /tmp, writable by
# every user) then `cat > $ODOO_RC` opens the destination with O_TRUNC
# which only needs write access to the FILE, not the parent dir.
TEMP_RC=$(mktemp)
trap "rm -f \"$TEMP_RC\"" EXIT

cp "$TEMPLATE_CONF" "$TEMP_RC"

# Second pass: replace each ${VAR} placeholder with the resolved value.
while IFS='=' read -r key value || [[ -n $key ]]; do
    # Skip comments and empty lines
    [[ $key =~ ^#.* ]] || [[ -z $key ]] && continue

    value=${!key} # Get the value of the variable whose name is $key

    # Escape characters which are special to sed
    value_escaped=$(echo "$value" | sed 's/[\/&]/\\&/g')

    # Substitute in the temp file (whose parent dir IS writable).
    sed -i "s/\${$key}/${value_escaped}/g" "$TEMP_RC"
done < .env

# Truncate-write the target. Requires write access to the FILE only;
# the Dockerfile pre-creates it with odoo ownership at build time.
cat "$TEMP_RC" > "$ODOO_RC"

echo "Configuration file is generated at $ODOO_RC"
