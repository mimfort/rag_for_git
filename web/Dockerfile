# Многоступенчатая сборка веб-админки наблюдаемости.
# Stage 1: node собирает React/Vite SPA в dist.
# Stage 2: python-slim отдаёт собранный SPA + FastAPI JSON-API (uvicorn).
# Образ лёгкий: web-приложению нужны только fastapi/uvicorn/psycopg/pydantic-settings
# (тяжёлые зависимости агента — langchain/voyage/neo4j/tree-sitter — не импортируются).

# --- Stage 1: сборка фронтенда ---
FROM node:20-alpine AS frontend
WORKDIR /app/web/frontend
COPY web/frontend/package.json web/frontend/package-lock.json ./
RUN npm ci
COPY web/frontend/ ./
RUN npm run build          # → /app/web/frontend/dist

# --- Stage 2: бэкенд, раздающий SPA + API ---
FROM python:3.12-slim AS backend
WORKDIR /app
# Только рантайм-зависимости веб-слоя (не весь reviewer)
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "psycopg[binary]>=3.2" "pydantic-settings>=2.3"
# Исходники reviewer (импортируются только reviewer.config + reviewer.web; остальное лежит без импорта)
COPY reviewer/ ./reviewer/
# Собранный фронт из stage 1 — путь совпадает с _FRONTEND_DIST в reviewer/web/app.py
COPY --from=frontend /app/web/frontend/dist ./web/frontend/dist
ENV PYTHONPATH=/app
EXPOSE 8000
# create_app(Settings()) читает PG_DSN из окружения (задаётся в docker-compose)
CMD ["python", "-c", "import uvicorn; from reviewer.config.settings import Settings; from reviewer.web.app import create_app; uvicorn.run(create_app(Settings()), host='0.0.0.0', port=8000)"]
