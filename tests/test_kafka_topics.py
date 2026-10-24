from types import SimpleNamespace

from confluent_kafka import KafkaError, KafkaException

from aiot_pipeline.kafka_topics import ensure_topics, wait_for_broker
from aiot_pipeline.topics import TOPIC_SETTINGS, KafkaTopic


class FakeFuture:
    def __init__(self, error: KafkaError | None = None) -> None:
        self.error = error

    def result(self) -> None:
        if self.error is not None:
            raise KafkaException(self.error)


class FakeAdmin:
    def __init__(self, exists_on_create: set[str] = frozenset()) -> None:
        self.requested = []
        self.exists_on_create = exists_on_create
        self.calls = 0

    def list_topics(self, timeout: float):
        self.calls += 1
        if self.calls == 1:
            raise KafkaException(KafkaError(KafkaError._TRANSPORT))
        return SimpleNamespace(brokers={1: object()}, topics={"gas.sensor.readings.v1": None})

    def create_topics(self, topics, operation_timeout):
        self.requested.extend(topics)
        return {
            topic.topic: FakeFuture(
                KafkaError(KafkaError.TOPIC_ALREADY_EXISTS)
                if topic.topic in self.exists_on_create
                else None
            )
            for topic in topics
        }


def test_every_topic_has_partitions_and_retention() -> None:
    assert set(TOPIC_SETTINGS) == set(KafkaTopic)
    assert TOPIC_SETTINGS[KafkaTopic.SENSOR] == (3, 604_800_000)
    assert TOPIC_SETTINGS[KafkaTopic.DEAD_LETTER] == (1, 1_209_600_000)


def test_waits_for_the_broker_then_creates_only_missing_topics() -> None:
    admin = FakeAdmin(exists_on_create={"gas.dead-letter.v1"})
    existing = wait_for_broker(admin, timeout_s=5)
    assert admin.calls == 2 and existing == {"gas.sensor.readings.v1"}
    created = ensure_topics(admin, existing)
    names = {topic.topic for topic in admin.requested}
    assert "gas.sensor.readings.v1" not in names and len(names) == len(TOPIC_SETTINGS) - 1
    # Created concurrently by someone else: fine, it is not reported as created here.
    assert "gas.dead-letter.v1" not in created and "gas.predictions.v1" in created
    prediction = next(t for t in admin.requested if t.topic == "gas.predictions.v1")
    assert prediction.num_partitions == 3
    assert prediction.config == {"retention.ms": "2592000000"}


def test_nothing_to_create_on_a_restart() -> None:
    admin = FakeAdmin()
    assert ensure_topics(admin, {str(topic) for topic in KafkaTopic}) == []
    assert admin.requested == []
