FROM python:3.11-slim

WORKDIR /app

# Системные зависимости: gcc + libssl нужны для сборки eth-account,
# curl — для HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev curl && \
    rm -rf /var/lib/apt/lists/*

# Копируем только requirements.txt сначала — используем кэш слоёв Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект (файлы не попавшие в .dockerignore)
COPY . .

# Директории для логов и данных (монтируются как volumes)
RUN mkdir -p /app/logs /app/data && \
    useradd -m pmts && \
    chown -R pmts:pmts /app

USER pmts

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/ready || exit 1

ENTRYPOINT ["python", "main.py"]
CMD ["--mode", "paper"]
