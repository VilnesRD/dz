FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update -q && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Зависимости Python — отдельным слоем для кэша
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения
COPY app/ ./app/
COPY scripts/ ./scripts/

# Директория для SQLite и данных
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
