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

# Install bash
RUN apk add --no-cache bash jq sqlite

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

# Expose the necessary port
EXPOSE 80
