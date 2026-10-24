"""kafka-init: wait for the broker, then create the missing pipeline topics in one admin call.

The previous shell script started one JVM per `kafka-topics.sh` call (list + 6 creates): about
45 s on a laptop while Kafka itself was still warming up, and every service behind it waited.
librdkafka's admin client polls the broker every half second and creates all topics in one
request, so the pipeline starts as soon as the broker answers.
"""

import logging
import sys
import time

from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import AdminClient, NewTopic

from .config import KafkaConfig
from .topics import TOPIC_SETTINGS

LOG = logging.getLogger("kafka-init")


def wait_for_broker(admin: AdminClient, timeout_s: float) -> set[str]:
    """Existing topic names once a broker answers; raises TimeoutError after timeout_s."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            metadata = admin.list_topics(timeout=2)
            if metadata.brokers:
                return set(metadata.topics)
        except KafkaException as error:
            LOG.info("Broker not ready yet: %s", error)
        if time.monotonic() > deadline:
            raise TimeoutError(f"no Kafka broker answered within {timeout_s:.0f} s")
        time.sleep(0.5)


def ensure_topics(admin: AdminClient, existing: set[str]) -> list[str]:
    """Create every topic of TOPIC_SETTINGS that is missing; returns the created names."""
    missing = [
        NewTopic(
            str(topic),
            num_partitions=partitions,
            replication_factor=1,
            config={"retention.ms": str(retention_ms)},
        )
        for topic, (partitions, retention_ms) in TOPIC_SETTINGS.items()
        if str(topic) not in existing
    ]
    if not missing:
        return []
    created = []
    for name, future in admin.create_topics(missing, operation_timeout=15).items():
        try:
            future.result()
            created.append(name)
        except KafkaException as error:
            # Another kafka-init (or a restart) got there first: that is the desired state.
            if error.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    started = time.monotonic()
    admin = AdminClient({"bootstrap.servers": KafkaConfig().bootstrap_servers})
    try:
        existing = wait_for_broker(admin, timeout_s=120)
        created = ensure_topics(admin, existing)
    except (TimeoutError, KafkaException) as error:
        LOG.error("Could not prepare the Kafka topics: %s", error)
        sys.exit(1)
    kept = len(TOPIC_SETTINGS) - len(created)
    LOG.info(
        "Topics ready in %.1f s: created %s, %d already existed",
        time.monotonic() - started,
        ", ".join(created) or "none",
        kept,
    )


if __name__ == "__main__":
    main()
