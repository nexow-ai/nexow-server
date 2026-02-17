FROM node:20-slim

WORKDIR /app

# pnpm via corepack
RUN corepack enable

COPY package.json pnpm-lock.yaml tsconfig.json ./
RUN pnpm install --frozen-lockfile

COPY src ./src

ENV SANDBOX_PORT=3001
EXPOSE 3001

CMD ["pnpm", "start"]
