# syntax=docker/dockerfile:1
#
# The demo surface: a Vite single-page app, built once and then served as flat
# files by nginx.
#
# Two stages, for the same reason the Python image has two: node, npm and the
# whole node_modules tree are build-time inputs, not runtime ones. A Vite build
# emits static assets — once `npm run build` has run, nothing in the container
# needs a JavaScript runtime, and shipping one would mean carrying several
# hundred megabytes of toolchain to serve a directory. The runtime stage is
# nginx and the compiled `dist/`, nothing else.
#
# nginx is not incidental here either: it is also the reverse proxy that makes
# the browser see the API and the app on one origin, which is what lets this
# repo have no CORS middleware anywhere. See nginx.conf.

# ---------------------------------------------------------------------------
# Stage 1 — build the static bundle
# ---------------------------------------------------------------------------
FROM node:22-alpine AS builder

WORKDIR /app

# Manifests first, install second, source last. Docker invalidates a layer and
# everything after it, so copying the source before installing would re-run the
# whole dependency install on every edit to a component. Dependencies change
# rarely; source changes constantly. This ordering is what keeps a UI edit a
# few-second rebuild instead of a few-minute one.
COPY package.json package-lock.json ./

# `npm ci` rather than `npm install`: it installs exactly the lockfile, fails
# if package.json and the lock disagree, and never silently rewrites the lock
# inside the build. A build that can change its own inputs is not reproducible.
#
# The install is then parked outside the workdir, because the `COPY . .` below
# would otherwise bury it under whatever node_modules the developer has on
# their laptop. That is not merely wasteful — this image is musl (alpine) and
# most laptops are glibc, and the packages that differ are exactly the ones
# that matter: esbuild and rollup ship per-platform native binaries, so a
# glibc node_modules copied into an alpine image fails at `vite build` with a
# missing-binary error that reads like a broken lockfile.
RUN npm ci && mv node_modules /node_modules

COPY . .

# Restore the install we just parked, discarding anything the copy brought in.
# `dist/` goes too: a stale local build must never be mistaken for this one's
# output, or the image silently serves whatever was last built by hand.
RUN rm -rf node_modules dist && mv /node_modules node_modules

RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — serve
# ---------------------------------------------------------------------------
FROM nginxinc/nginx-unprivileged:alpine AS runtime

# Replaces the stock default.conf. The stock file serves files but does not
# proxy /api/ and does not disable proxy buffering, so keeping it would break
# the streaming demo in a way that looks like a frontend bug.
COPY nginx.conf /etc/nginx/conf.d/default.conf

COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080

# Repeat the upstream image's numeric user explicitly: static policy scanners
# can then verify the runtime stage without trusting mutable image metadata.
USER 101

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=6 \
    CMD ["wget", "--quiet", "--spider", "http://127.0.0.1:8080/"]
