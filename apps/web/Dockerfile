FROM node:22-alpine AS base
WORKDIR /app

FROM base AS deps
COPY package.json package-lock.json ./
RUN npm ci

FROM deps AS source
COPY . .

FROM source AS lint
RUN npm run lint && npm run typecheck

FROM source AS builder
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM base AS runner
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOST=0.0.0.0 \
    PORT=3000

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

COPY --from=builder --chown=nextjs:nodejs /app/dist/standalone ./
# vinext 1.0.0-beta.3 does not trace these peer runtime packages into standalone output.
COPY --from=deps --chown=nextjs:nodejs /app/node_modules/react ./node_modules/react
COPY --from=deps --chown=nextjs:nodejs /app/node_modules/react-dom ./node_modules/react-dom
COPY --from=deps --chown=nextjs:nodejs /app/node_modules/scheduler ./node_modules/scheduler

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
