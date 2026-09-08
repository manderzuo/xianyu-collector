FROM python:3.11-slim

ARG APP_VERSION=
ARG APP_BUILD_ID=
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY scheduler/requirements.txt /app/scheduler/requirements.txt
RUN pip install --no-cache-dir -r /app/scheduler/requirements.txt
COPY common /app/common
COPY scheduler /app/scheduler
COPY VERSION.txt /app/VERSION.txt
COPY BUILD_ID.txt /app/BUILD_ID.txt
RUN if [ -n "$APP_VERSION" ]; then printf '%s\n' "$APP_VERSION" > /app/VERSION.txt; fi \
    && if [ -n "$APP_BUILD_ID" ]; then printf '%s\n' "$APP_BUILD_ID" > /app/BUILD_ID.txt; fi
ENV PYTHONPATH=/app
EXPOSE 8091
CMD ["uvicorn", "scheduler.main:app", "--host", "0.0.0.0", "--port", "8091"]
