ARG POSTGRES_TAG
FROM postgres:${POSTGRES_TAG}

#------------------------#
#    APT Dependencies    #
#------------------------#
# Update apt packages
RUN apt-get update && apt-get install -y \
    # Unaccent extension dependencies
    postgresql-contrib \
    apt-utils \
    # Clean up the apt cache to reduce the image size
    && rm -rf /var/lib/apt/lists/*

# `.env` is bind-mounted at runtime by docker-compose (see /.env volume),
# not baked into the image. Baking it puts every Postgres / Odoo / pgAdmin
# secret into the image layer and `docker history` output — anyone who can
# pull the image (or any registry that mirrors it) can extract them.

# Copy the script to create the unaccent template
COPY ./postgres/entrypoint.sh /docker-entrypoint-initdb.d/entrypoint.sh
