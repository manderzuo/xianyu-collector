FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY scheduler/requirements.txt /app/scheduler/requirements.txt
RUN pip install --no-cache-dir -r /app/scheduler/requirements.txt
COPY common /app/common
COPY scheduler /app/scheduler
ENV PYTHONPATH=/app
EXPOSE 8091
CMD ["uvicorn", "scheduler.main:app", "--host", "0.0.0.0", "--port", "8091"]
