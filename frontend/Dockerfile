# syntax=docker/dockerfile:1
#
# Multi-stage build. Targets `dev` and `runtime` are referenced by name from
# docker-compose.override.yml and docker-compose.yml respectively — do not
# rename them.

# ---------------------------------------------------------------------------
# deps: install node_modules once. There is no package-lock.json at the
# start of the build (Wave-1 agents haven't added deps yet), so this falls
# back to `npm install`; once a lockfile exists, this becomes reproducible
# via `npm ci` automatically.
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN [ -f package-lock.json ] && npm ci || npm install

# ---------------------------------------------------------------------------
# dev: deps + source, Vite dev server. docker-compose.override.yml
# bind-mounts src/, public/ and index.html over this and supplies the
# actual `command:`; this CMD is the standalone default.
FROM node:22-alpine AS dev
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]

# ---------------------------------------------------------------------------
# build: production bundle. VITE_API_BASE is empty by default — same origin,
# because nginx (runtime stage) proxies /api to the backend so the browser
# never makes a cross-origin request.
FROM node:22-alpine AS build
ARG VITE_API_BASE=""
ENV VITE_API_BASE=${VITE_API_BASE}
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# ---------------------------------------------------------------------------
# runtime: nginx serving the built SPA. Non-root *where practical*: nginx
# needs root in the master process to bind port 80, but it drops every
# worker (the processes that actually handle requests) to the low-privilege
# `nginx` user via the `user nginx;` directive already set in the base
# image's nginx.conf. We don't override that — forcing a full non-root
# USER here would break the port-80 bind that docker-compose.yml expects.
FROM nginx:alpine AS runtime
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
