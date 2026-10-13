FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
COPY pyproject.toml README.md ./
COPY assistant ./assistant
RUN pip install . \
    && useradd --create-home --uid 1000 app \
    && mkdir /data && chown app /data

USER app
WORKDIR /data

# Listen on all interfaces *inside* the container so Docker can forward traffic in.
# docker-compose.yml publishes the port on the host's 127.0.0.1 only.
ENV ASSISTANT_HOST=0.0.0.0 \
    ASSISTANT_DB=/data/memories.db

VOLUME /data
EXPOSE 8000
CMD ["python", "-m", "assistant.server"]
