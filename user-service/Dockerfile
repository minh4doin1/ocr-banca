# ============================================================
# Dockerfile — user-service (Node.js + Fastify + keycloak-admin)
# Multi-target:
#   - dev:     hot reload, expects source bind-mounted
#   - builder: build TS
#   - runtime: production image (final stage)
# ============================================================

# ── Base ──
FROM node:20-alpine AS base

WORKDIR /app

# Install deps (cache layer dùng lại khi đổi src)
COPY package*.json tsconfig.json ./
RUN npm ci

# ── Dev target ──
# Hot reload qua `tsx watch`. Source code phải được bind-mount
# (xem docker-compose.yml) — KHÔNG COPY src/ ở đây.
FROM base AS dev

USER node

ENV NODE_ENV=development
ENV PATH=/app/node_modules/.bin:$PATH

EXPOSE 8300

CMD ["npx", "tsx", "watch", "--clear-screen=false", "src/index.ts"]

# ── Builder ──
FROM base AS builder

COPY src/ ./src/
RUN npm run build

# ── Runtime (production) ──
FROM node:20-alpine AS runtime

WORKDIR /app

# Copy node_modules + built code
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY package.json ./

# Non-root user (Alpine có sẵn `node` user uid 1000)
USER node

ENV NODE_ENV=production

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:8300/healthz || exit 1

CMD ["node", "dist/index.js"]