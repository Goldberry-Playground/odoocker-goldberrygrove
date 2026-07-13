# Custom Caddy image with the DigitalOcean DNS plugin baked in.
#
# Why this exists: PR #82 switched QA to DNS-01 wildcard TLS but referenced
# `slothcored/caddy:digitalocean`, which turned out to not exist on Docker
# Hub (verified 2026-06-26 -- 404). The first deploy attempt failed in
# compose-up because the image couldn't be pulled. The preview env had the
# same dangling reference but had never actually been deployed, so the bug
# stayed hidden until QA tried to use it.
#
# Canonical Caddy plugin pattern per https://caddyserver.com/docs/build:
# multi-stage build with caddy:builder + xcaddy + the desired plugin module,
# then copy the resulting binary into the standard caddy runtime image.
#
# Build target: ~50 MB final image; build itself takes ~30s on first compose-up.
# Subsequent compose-ups are instant (Docker layer cache).

# Audit fix DL3007 (2026-06-29): pin to the major-version tag instead of
# `:latest`/`:builder` (which IS effectively :latest). caddy:2-builder +
# caddy:2 give us reproducible behavior across rebuilds. Bumping to Caddy 3
# becomes an intentional decision (edit this Dockerfile + verify upstream
# plugin compatibility). caddy:2-builder floats to the latest Caddy 2.x, which
# is >= 2.10 -- required by the pinned plugin below (libdns v1.0.0 interface).
FROM caddy:2-builder AS builder
# GOL-345: pin caddy-dns/digitalocean to a commit PAST the libdns cleanup fix.
#
# The plugin was previously UNPINNED (`--with .../digitalocean`). Two problems:
#   1. Reproducibility: `@latest` drifts; a cached `RUN xcaddy build` layer can
#      freeze an OLD resolution. This shared image (QA + prod + preview) was
#      last built from a layer predating the 2025-06-06 upstream fix, so every
#      fresh preview droplet inherited the buggy plugin.
#   2. Correctness: the pre-2025-06 libdns/digitalocean failed to DELETE the
#      temporary _acme-challenge TXT after DNS-01 (strconv.Atoi on an empty
#      record ID -> "invalid syntax"; libdns/digitalocean#4 class). The stale
#      TXT tripped Let's Encrypt secondary validation, prod issuance failed,
#      and the Caddyfile multi-issuer fallback landed on LE *staging* (browser
#      "not trusted" warning). It recurred on every preview boot.
#
# Commit 04bde2867106 (2025-06-06) is caddy-dns/digitalocean master AND its
# current `@latest`; its go.mod requires the refactored libdns/digitalocean
# (dfa7af5, which preserves the record ID so cleanup no longer Atoi's "") and
# caddy v2.10.0 + libdns/libdns v1.0.0. Pinning here also cache-busts the stale
# xcaddy layer, forcing a fresh module resolution to the fixed code.
RUN xcaddy build \
    --with github.com/caddy-dns/digitalocean@v0.0.0-20250606074528-04bde2867106

FROM caddy:2
# CI-unblock (incidental to GOL-345, unrelated to the cert logic): the floating
# caddy:2 Alpine base currently ships c-ares 1.34.6-r0, which the Trivy gate
# (docker-caddy.yml, ignore-unfixed=true) flags as HIGH CVE-2026-33630 -- fixed
# upstream in 1.34.8-r0. A fixable HIGH blocks the build, so ANY grove-caddy
# rebuild fails here until the caddy base catches up. Pull the patched OS
# package explicitly (`apk upgrade`, not `apk add`, so no exact-pin churn) --
# the caddy binary itself scans clean (0 gobinary vulns).
RUN apk upgrade --no-cache c-ares
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
