# Stage 1 — build the React frontend
FROM node:20-alpine AS frontend
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src/ ./src/
RUN npm run build

# Stage 2 — run the FastAPI backend + serve the built frontend
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api.py uipath_auth.py admin.html dashboard.html ./
COPY consensus/ ./consensus/
COPY --from=frontend /app/dist ./dist

# Render injects $PORT at runtime
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
