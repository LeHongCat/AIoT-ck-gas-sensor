import os
from dataclasses import dataclass


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MqttConfig:
    host: str = os.getenv("MQTT_HOST", "localhost")
    port: int = int(os.getenv("MQTT_PORT", "1883"))
    username: str | None = os.getenv("MQTT_USERNAME")
    password: str | None = os.getenv("MQTT_PASSWORD")
    qos: int = int(os.getenv("MQTT_QOS", "1"))


@dataclass(frozen=True)
class KafkaConfig:
    bootstrap_servers: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:29092")
    consumer_group: str = os.getenv("KAFKA_CONSUMER_GROUP", "influx-sink-v1")


@dataclass(frozen=True)
class InfluxConfig:
    url: str = os.getenv("INFLUXDB_URL", "http://localhost:8086")
    token: str = os.getenv("INFLUXDB_TOKEN", "dev-super-secret-token-change-me")
    org: str = os.getenv("INFLUXDB_ORG", "aiot")
    bucket: str = os.getenv("INFLUXDB_BUCKET", "gas_monitoring")
    batch_size: int = int(os.getenv("INFLUX_BATCH_SIZE", "200"))
    flush_seconds: float = float(os.getenv("INFLUX_FLUSH_SECONDS", "0.5"))
