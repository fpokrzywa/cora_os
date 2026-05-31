FROM node:20-alpine AS build

WORKDIR /app

ENV NODE_ENV=production

COPY package.json ./
RUN npm install --include=dev --no-audit --no-fund

COPY tsconfig.json tsconfig.node.json vite.config.ts index.html ./
COPY src ./src

ARG VITE_CORA_API_URL=http://api.cora.local.arpa
ENV VITE_CORA_API_URL=$VITE_CORA_API_URL

RUN npm run build

FROM nginx:1.27-alpine AS runtime

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost/ || exit 1
