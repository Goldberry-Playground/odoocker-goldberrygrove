#------------------------#
#     PGAdmin Server     #
#------------------------#
ARG PGADMIN_TAG
FROM dpage/pgadmin4:${PGADMIN_TAG}

# Receive ARGs from docker-compose.yml & convert them into ENVs
ARG PGADMIN_DEFAULT_EMAIL

ENV PGADMIN_DEFAULT_EMAIL=${PGADMIN_DEFAULT_EMAIL}

# Switch to root user
USER root

# Update Alpine OS packages to pull current security patches (clears
# upstream alpine CVEs whose fix has shipped since the pgadmin image was
# built), then install our extras. apk upgrade is the real fix for any
# OS-package finding Trivy reports against the pgadmin Alpine layer.
RUN apk update && apk upgrade --no-cache && \
    apk add --no-cache bash jq sqlite

# Force-upgrade urllib3 inside pgadmin's bundled Python venv. pgadmin
# 9.15 ships urllib3 2.6.3; CVE-2026-44432 (HIGH) is fixed in 2.7.0.
# Upstream pgadmin will ship 2.7.0 in a future release; until then we
# pin a current version here. The venv was created with --without-pip
# and --system-site-packages, so we install via the system pip targeting
# the venv's site-packages directory. Old dist-info is removed first or
# Trivy still flags the leftover 2.6.3 metadata.
ARG PGADMIN_VENV_SITE_PACKAGES=/venv/lib/python3.14/site-packages
RUN rm -rf "${PGADMIN_VENV_SITE_PACKAGES}/urllib3" \
           "${PGADMIN_VENV_SITE_PACKAGES}/urllib3-2.6.3.dist-info" && \
    /usr/local/bin/pip3 install \
        --no-cache-dir \
        --target="${PGADMIN_VENV_SITE_PACKAGES}" \
        --root-user-action=ignore \
        'urllib3>=2.7.0,<3.0'

# Check if the private key exists, copy it and set permissions if it does
RUN if [ -f ./pgadmin/private_key ]; then \
        cp ./pgadmin/private_key /pgadmin4/private_key && \
        chown pgadmin:root /pgadmin4/private_key && \
        chmod 600 /pgadmin4/private_key; \
    fi

# Copy the start script. `.env` is NOT baked here — it's bind-mounted at
# runtime by docker-compose (see /.env volume). Baking it would put every
# secret into the image layer + every registry that mirrors it.
COPY ./pgadmin/start_pgadmin.sh /var/lib/pgadmin/start_pgadmin.sh

# Make the script executable.
RUN chmod +x /var/lib/pgadmin/start_pgadmin.sh

# Defer `start_pgadmin.sh` to container start — at BUILD time .env isn't
# available (intentionally), and running it at build also baked the
# generated servers.json (including the SSH `private_key` path) into the
# image layer. At runtime the env is present via the bind mount and the
# generated files live in the writable container layer, not the image.
ENTRYPOINT ["/var/lib/pgadmin/start_pgadmin.sh"]

# Drop back to the unprivileged upstream user (UID 5050) so the runtime
# container doesn't execute as root — hadolint DL3002. The root steps
# above (apk, pip into the venv site-packages, private-key chown) are
# all build-time only; nothing in start_pgadmin.sh requires root.
USER pgadmin

# Expose the necessary port
EXPOSE 80
