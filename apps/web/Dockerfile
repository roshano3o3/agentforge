# AgentForge dashboard, production build (`next build` + `next start`). Used by docker-compose.yml so the
# Quick Start needs only Docker; `scripts/dev-web.ps1` still runs the dev server natively for development.
FROM node:22-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# Read by the BROWSER (the dashboard calls the API from the page), so it's the host's address of the API,
# not the compose network's. Baked in at build time, as Next.js does for NEXT_PUBLIC_* variables.
ARG NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}
RUN npx next build

FROM node:22-alpine
WORKDIR /web
ENV NODE_ENV=production
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=build /web/.next ./.next
COPY next.config.ts ./
EXPOSE 3000
CMD ["npx", "next", "start", "-H", "0.0.0.0", "-p", "3000"]
