FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY websocket/requirements.txt /app/websocket/requirements.txt
RUN pip install --no-cache-dir -r /app/websocket/requirements.txt
RUN playwright install --with-deps chromium
COPY common /app/common
COPY websocket /app/websocket
ENV PYTHONPATH=/app
EXPOSE 8090
CMD ["uvicorn", "websocket.main:app", "--host", "0.0.0.0", "--port", "8090"]
