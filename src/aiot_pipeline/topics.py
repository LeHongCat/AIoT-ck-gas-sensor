from enum import StrEnum


class KafkaTopic(StrEnum):
    SENSOR = "gas.sensor.readings.v1"
    PREDICTION = "gas.predictions.v1"
    GROUND_TRUTH = "gas.ground-truth.v1"
    CONTROL_REQUEST = "gas.control-requests.v1"
    ACTUATOR_COMMAND = "gas.actuator-commands.v1"
    ACTUATOR_STATE = "gas.actuator-state.v1"
    DEAD_LETTER = "gas.dead-letter.v1"


DAY_MS = 24 * 60 * 60 * 1000

# Partitions and time retention of every topic (kafka-init creates them, see kafka_topics.py).
# Size retention (256 MiB per partition) comes from the broker default in compose.yaml.
TOPIC_SETTINGS: dict[KafkaTopic, tuple[int, int]] = {
    KafkaTopic.SENSOR: (3, 7 * DAY_MS),
    KafkaTopic.PREDICTION: (3, 30 * DAY_MS),
    KafkaTopic.GROUND_TRUTH: (3, 30 * DAY_MS),
    KafkaTopic.CONTROL_REQUEST: (3, 30 * DAY_MS),
    KafkaTopic.ACTUATOR_COMMAND: (3, 30 * DAY_MS),
    KafkaTopic.ACTUATOR_STATE: (3, 30 * DAY_MS),
    KafkaTopic.DEAD_LETTER: (1, 14 * DAY_MS),
}


MQTT_SUBSCRIPTIONS = (
    "aiot/v1/nodes/+/readings",
    "aiot/v1/nodes/+/predictions",
    "aiot/v1/nodes/+/ground-truth",
    "aiot/v1/nodes/+/control-requests",
    "aiot/v1/nodes/+/commands",
    "aiot/v1/nodes/+/actuator-state",
)


def mqtt_topic(node_id: str, event_type: str) -> str:
    return f"aiot/v1/nodes/{node_id}/{event_type}"


MQTT_SUFFIX_TO_KAFKA = {
    "readings": KafkaTopic.SENSOR,
    "predictions": KafkaTopic.PREDICTION,
    "ground-truth": KafkaTopic.GROUND_TRUTH,
    "control-requests": KafkaTopic.CONTROL_REQUEST,
    "commands": KafkaTopic.ACTUATOR_COMMAND,
    "actuator-state": KafkaTopic.ACTUATOR_STATE,
}
