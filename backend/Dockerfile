FROM python:3.11-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt
COPY common /app/common
COPY backend /app/backend
RUN mkdir -p /app/static/uploads /app/backups
ENV PYTHONPATH=/app
EXPOSE 8089
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8089"]
