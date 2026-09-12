# RevenueOS: Python core + Node (orchestrator, connector CLIs) in one image.
FROM node:22-bookworm-slim AS node
FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && pip install --no-cache-dir uv \
 && apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Everything hatchling force-includes into the wheel must exist before `uv sync`
# (the workspace template: skills, agents, tools, canon, playbooks, website, orchestrator src).
COPY pyproject.toml uv.lock README.md VENDOR.json NOTICE.md ./
COPY src ./src
COPY orchestrator/package.json orchestrator/package-lock.json orchestrator/tsconfig.json ./orchestrator/
COPY orchestrator/src ./orchestrator/src
COPY skills ./skills
COPY agents ./agents
COPY tools ./tools
COPY methodology ./methodology
COPY playbooks ./playbooks
COPY company-context ./company-context
COPY learning-loop ./learning-loop
COPY capabilities ./capabilities
COPY website ./website
COPY data/automations.json ./data/automations.json
RUN uv sync --frozen --no-dev
RUN cd orchestrator && npm ci --omit=dev
# Only the schedule definition ships baked in — everything else under data/ (the SQLite
# store, exports, outputs) is workspace state on the ./data volume (see docker-compose.yml).

# revenueos is on PATH via .venv/bin (from `uv sync` above); the panel's
# GET /health returns 200 JSON once onboarded or not — either way it answers,
# so this only fails when the panel process itself is down or unreachable.
ENV PATH="/app/.venv/bin:${PATH}" REVENUEOS_ROOT=/app IS_SANDBOX=1 TZ=UTC
EXPOSE 8790 8791
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8791/health || exit 1
CMD ["revenueos", "doctor"]
