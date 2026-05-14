#------------------------#
#     Odoo Community     #
#------------------------#
ARG ODOO_TAG
FROM odoo:${ODOO_TAG}

# Switch to root user
USER root

# Build-time args. ENTERPRISE_USER / ENTERPRISE_ACCESS_TOKEN / GITHUB_USER /
# GITHUB_ACCESS_TOKEN are intentionally NOT promoted to ENV — they're only
# needed by clone-addons.sh below. Keeping them as ARG means they vanish
# after the build completes and never appear in the final image's history
# or `docker inspect` output.
ARG ODOO_TAG
ARG ROOT_PATH
ARG LOG_PATH
ARG ENTERPRISE_USER
ARG ENTERPRISE_ACCESS_TOKEN
ARG ENTERPRISE_ADDONS
ARG GITHUB_USER
ARG GITHUB_ACCESS_TOKEN
ARG THIRD_PARTY_ADDONS
ARG ODOO_RC
ARG USE_REDIS
ARG USE_S3
ARG USE_SENTRY

# Only the non-sensitive ARGs get promoted to ENV.
ENV ODOO_TAG=${ODOO_TAG} \
    LOG_PATH=${LOG_PATH} \
    ENTERPRISE_USER=${ENTERPRISE_USER} \
    ENTERPRISE_ADDONS=${ENTERPRISE_ADDONS} \
    GITHUB_USER=${GITHUB_USER} \
    THIRD_PARTY_ADDONS=${THIRD_PARTY_ADDONS} \
    ODOO_RC=${ODOO_RC} \
    USE_REDIS=${USE_REDIS} \
    USE_S3=${USE_S3} \
    USE_SENTRY=${USE_SENTRY}

#---------------------#
#       Logging       #
#---------------------#
# Create odoo.log file
RUN touch ${LOG_PATH} && chown odoo:odoo ${LOG_PATH}

#------------------------#
#    APT Dependencies    #
#------------------------#
# Install Odoocker image dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    zip \
    unzip \
    git \
    openssh-client \
    patch \
    && rm -rf /var/lib/apt/lists/*

#---------------------#
#   PIP Dependecies   #
#---------------------#
# Copy & Install PIP requirements
COPY --chown=odoo:odoo ./odoo/requirements.txt /tmp/requirements.txt

RUN python3 -m pip install --break-system-packages -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

#--------------------------------------------#
#    Odoo Enterprise + Third Party Addons    #
#--------------------------------------------#
# Create third-party-addons directory and clone them
COPY --chown=odoo:odoo ./odoo/clone-addons.sh /
COPY --chown=odoo:odoo ./odoo/third-party-addons.txt /
RUN /clone-addons.sh

#-----------------------#
#       Odoo Conf       #
#-----------------------#
# Copy the odoo.conf template + the rendering script. Critically, we do
# NOT COPY ./.env / here — that would bake every secret in .env into the
# image's filesystem and history (visible via `docker history` / `docker
# inspect`, plus any registry that caches the layer). Instead, env vars
# are injected at runtime via docker-compose, and the entrypoint runs
# /odoorc.sh on container start to substitute them into /odoo.conf.
COPY --chown=odoo:odoo ./odoo/odoo.conf /
COPY --chown=odoo:odoo ./odoo/odoorc.sh /

# Pre-create ${ODOO_RC} with odoo ownership so the runtime odoorc.sh
# (which executes as the `odoo` user) can rewrite it. Without this the
# script's `cp /odoo.conf $ODOO_RC` fails with "Permission denied" on
# the default Odoo image where /usr/lib/python3/dist-packages/odoo/ is
# root-owned. We seed with the literal template so the file is valid
# even before the first substitution pass; subsequent boots overwrite.
RUN install -D -o odoo -g odoo -m 644 /odoo.conf "${ODOO_RC}"

# Switch back to odoo user
USER odoo
