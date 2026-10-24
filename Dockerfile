FROM python:3.12.12-slim

# MALLOC_ARENA_MAX=2: glibc otherwise grows one malloc arena per thread (Kafka/MQTT client
# threads), which inflates resident memory for no benefit at this load.
# OPENBLAS_NUM_THREADS=1: the TabM matmuls are tiny and each container gets a fraction of a CPU;
# one BLAS thread per core would only fight over that quota.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    OPENBLAS_NUM_THREADS=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 appuser     && mkdir -p /logs && chown appuser /logs

# Dependencies change rarely: install them in their own layer from pyproject.toml only, so a
# code or UI edit rebuilds in seconds instead of reinstalling Kafka/Influx/FastAPI clients.
COPY pyproject.toml ./
RUN mkdir -p src/aiot_pipeline \
    && touch src/aiot_pipeline/__init__.py \
    && pip install . \
    && pip uninstall -y aiot-gas-pipeline \
    && rm -rf src build

COPY src ./src
RUN pip install --no-deps . && rm -rf build

USER appuser
CMD ["python", "-m", "aiot_pipeline.mqtt_kafka_bridge"]
