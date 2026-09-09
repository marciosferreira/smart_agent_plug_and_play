FROM node:20-slim AS frontend-build

WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM python:3.11-slim

WORKDIR /app

# Dependências do sistema necessárias para scipy/numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código
COPY . .

# Build do frontend Angular, gerado no estágio anterior
COPY --from=frontend-build /app/frontend/dist/frontend/browser ./frontend/dist/frontend/browser

# Diretório para o banco SQLite e credentials — monte um volume aqui no EasyPanel
RUN mkdir -p /data

# Variáveis de ambiente com defaults seguros
ENV DB_PATH=/data/mfg.db \
    GOOGLE_APPLICATION_CREDENTIALS=/data/credentials.json \
    LOCATION=us-central1 \
    MODEL_NAME=gemini-3.8-flash \
    BACKEND_URL=http://localhost:8000 \
    MAX_INTERACOES=10 \
    DEBUG_TOOLS=0

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
