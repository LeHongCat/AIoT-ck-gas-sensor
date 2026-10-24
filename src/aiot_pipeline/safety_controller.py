import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime

import paho.mqtt.client as mqtt
from confluent_kafka import Consumer, KafkaError, KafkaException
from pydantic import ValidationError

from .config import KafkaConfig, MqttConfig
from .contracts import ActuatorCommand, ControlRequest, Prediction
from .metrics import (
    ALARM_LEVEL,
    CONTROL_COMMANDS,
    CONTROL_DEDUPLICATED,
    CONTROL_LOOP_LAG,
    DESIRED_OUTPUT,
    OVERRIDE_ACTIVE,
    SIREN_MUTED,
    start_metrics_server,
)
from .topics import KafkaTopic, mqtt_topic

LOG = logging.getLogger("safety-controller")
ALARM_SEVERITY = {"normal": 0, "warning": 1, "danger": 2}


def automatic_outputs(alarm_level: str, siren_muted: bool = False) -> dict[str, bool]:
    """Map model alarm levels to deterministic fail-safe actuator outputs.

    A silenced siren only drops `alarm_on`: ventilation and the gas valve still follow the alarm.
    """
    active = alarm_level in {"warning", "danger"}
    return {
        "fan_on": active,
        "exhaust_on": active,
        "gas_valve_closed": alarm_level == "danger",
        "alarm_on": active and not siren_muted,
        # Fire suppression is never automatic: only an operator can discharge it.
        "suppression_on": False,
    }


def with_suppression(outputs: dict[str, bool]) -> dict[str, bool]:
    """Suppression interlock: discharging the hood suppression system always shuts the gas
    valve and sounds the alarm."""
    return {**outputs, "gas_valve_closed": True, "alarm_on": True, "suppression_on": True}


def manual_outputs(request: ControlRequest) -> dict[str, bool]:
    outputs = {
        "fan_on": request.fan_on,
        "exhaust_on": request.exhaust_on,
        "gas_valve_closed": request.gas_valve_closed,
        "alarm_on": request.alarm_on,
        "suppression_on": False,
    }
    return with_suppression(outputs) if request.suppression_on else outputs


def takeover_outputs(
    alarm_level: str, suppression_active: bool, siren_muted: bool = False
) -> dict[str, bool]:
    """Auto outputs when automation takes over. Danger may only add protection: a running
    suppression discharge is kept until its own deadline (and sounds the siren even if muted)."""
    outputs = automatic_outputs(alarm_level, siren_muted)
    return with_suppression(outputs) if suppression_active else outputs


def automation_should_take_over(alarm_level: str, override_deadline: float, now: float) -> bool:
    return alarm_level == "danger" or override_deadline <= now


def siren_rearms(previous_level: str | None, alarm_level: str) -> bool:
    """Every new DANGER sounds the siren again, even if an operator silenced it earlier."""
    return alarm_level == "danger" and previous_level != "danger"


class SafetyController:
    def __init__(self) -> None:
        mqtt_config = MqttConfig()
        kafka_config = KafkaConfig()
        self.qos = mqtt_config.qos
        self.stop_event = threading.Event()
        self.predictions: dict[str, Prediction] = {}
        self.last_outputs: dict[str, tuple[object, ...]] = {}
        self.sequences: dict[str, int] = {}
        self.override_until: dict[str, float] = {}
        self.suppression_until: dict[str, float] = {}
        # Siren silenced by an operator, per node. Kept in memory on purpose: a restarted
        # controller starts with every siren armed (fail-safe).
        self.siren_muted: dict[str, bool] = {}
        self.override_seconds = float(os.getenv("DIRECT_OVERRIDE_SECONDS", "30"))
        self.consumer = Consumer(
            {
                "bootstrap.servers": kafka_config.bootstrap_servers,
                "group.id": "safety-controller-v1",
                # Default pre-fetch queue is 64 MiB per consumer; a few events/s need far less.
                "queued.max.messages.kbytes": 4096,
                "enable.auto.commit": False,
                "auto.offset.reset": "latest",
                "isolation.level": "read_committed",
            }
        )
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="safety-controller-v1",
            protocol=mqtt.MQTTv5,
        )
        if mqtt_config.username:
            self.client.username_pw_set(mqtt_config.username, mqtt_config.password)
        self.mqtt_config = mqtt_config

    def _publish_command(
        self,
        node_id: str,
        mode: str,
        outputs: dict[str, bool],
        reason: str,
        trigger: str,
        alarm_muted: bool = False,
    ) -> None:
        signature = (mode, *outputs.values(), alarm_muted)
        if self.last_outputs.get(node_id) == signature:
            CONTROL_DEDUPLICATED.labels(node_id=node_id).inc()
            return
        sequence = self.sequences.get(node_id, 0)
        command = ActuatorCommand(
            node_id=node_id,
            observed_at=datetime.now(UTC),
            sequence=sequence,
            mode=mode,
            source="operator" if mode == "manual" else "safety-controller",
            reason=reason,
            alarm_muted=alarm_muted,
            **outputs,
        )
        result = self.client.publish(
            mqtt_topic(node_id, "commands"),
            command.model_dump_json(),
            qos=self.qos,
        )
        result.wait_for_publish(timeout=5)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError(f"MQTT command publish failed: {mqtt.error_string(result.rc)}")
        self.last_outputs[node_id] = signature
        self.sequences[node_id] = sequence + 1
        CONTROL_COMMANDS.labels(node_id=node_id, mode=mode, trigger=trigger).inc()
        for output, value in outputs.items():
            DESIRED_OUTPUT.labels(node_id=node_id, output=output).set(int(value))
        LOG.info("Published %s command for %s: %s", mode, node_id, reason)

    def _set_siren_muted(self, node_id: str, muted: bool) -> None:
        self.siren_muted[node_id] = muted
        SIREN_MUTED.labels(node_id=node_id).set(int(muted))

    def _publish_auto(self, node_id: str, alarm_level: str, trigger: str, note: str = "") -> None:
        """Auto outputs for the alarm level, with any suppression discharge and siren mute."""
        suppression = self.suppression_until.get(node_id, 0) > time.monotonic()
        if not suppression:
            self.suppression_until.pop(node_id, None)
        muted = self.siren_muted.get(node_id, False)
        reason = "; ".join(
            part
            for part in (
                note,
                f"prediction alarm level is {alarm_level}",
                "suppression discharge kept" if suppression else "",
                "siren muted by operator" if muted else "",
            )
            if part
        )
        self._publish_command(
            node_id,
            "auto",
            takeover_outputs(alarm_level, suppression, muted),
            reason,
            trigger=trigger,
            alarm_muted=muted,
        )

    def _handle_prediction(self, prediction: Prediction) -> None:
        node_id = prediction.node_id
        previous = self.predictions.get(node_id)
        self.predictions[node_id] = prediction
        CONTROL_LOOP_LAG.observe(
            max(0.0, datetime.now(UTC).timestamp() - prediction.observed_at.timestamp())
        )
        ALARM_LEVEL.labels(node_id=node_id).set(ALARM_SEVERITY[prediction.alarm_level])
        # Export the siren state for every node that predicts, not only after a first mute.
        SIREN_MUTED.labels(node_id=node_id).set(int(self.siren_muted.get(node_id, False)))
        note = ""
        if self.siren_muted.get(node_id) and siren_rearms(
            previous.alarm_level if previous else None, prediction.alarm_level
        ):
            self._set_siren_muted(node_id, False)
            note = "new danger re-armed the siren"
            LOG.warning("New DANGER on %s: siren re-armed", node_id)
        if not automation_should_take_over(
            prediction.alarm_level,
            self.override_until.get(node_id, 0),
            time.monotonic(),
        ):
            return
        self.override_until.pop(node_id, None)
        OVERRIDE_ACTIVE.labels(node_id=node_id).set(0)
        self._publish_auto(node_id, prediction.alarm_level, prediction.alarm_level, note)

    def _handle_request(self, request: ControlRequest) -> None:
        if request.mode in {"mute", "unmute"}:
            muted = request.mode == "mute"
            self._set_siren_muted(request.node_id, muted)
            LOG.info(
                "Siren %s for %s by operator", "muted" if muted else "unmuted", request.node_id
            )
            # A running direct override keeps its outputs; the setting applies when Auto resumes.
            prediction = self.predictions.get(request.node_id)
            if prediction and self.override_until.get(request.node_id, 0) <= time.monotonic():
                self._publish_auto(
                    request.node_id,
                    prediction.alarm_level,
                    request.mode,
                    "" if muted else "siren unmuted by operator",
                )
            return

        if request.mode == "manual":
            deadline = time.monotonic() + self.override_seconds
            self.override_until[request.node_id] = deadline
            if request.suppression_on:
                self.suppression_until[request.node_id] = deadline
            else:
                self.suppression_until.pop(request.node_id, None)
            OVERRIDE_ACTIVE.labels(node_id=request.node_id).set(1)
            self._publish_command(
                request.node_id,
                "manual",
                manual_outputs(request),
                f"direct intervention for {self.override_seconds:g} seconds",
                trigger="operator",
            )
            return

        self.override_until.pop(request.node_id, None)
        self.suppression_until.pop(request.node_id, None)
        OVERRIDE_ACTIVE.labels(node_id=request.node_id).set(0)
        if prediction := self.predictions.get(request.node_id):
            self._publish_auto(
                request.node_id,
                prediction.alarm_level,
                "cancel",
                "direct intervention cancelled",
            )

    def run(self) -> None:
        self.client.connect(self.mqtt_config.host, self.mqtt_config.port, keepalive=30)
        self.client.loop_start()
        self.consumer.subscribe([str(KafkaTopic.PREDICTION), str(KafkaTopic.CONTROL_REQUEST)])
        try:
            while not self.stop_event.is_set():
                message = self.consumer.poll(0.5)
                if message is None:
                    continue
                if message.error():
                    if message.error().code() != KafkaError._PARTITION_EOF:
                        raise KafkaException(message.error())
                    continue
                try:
                    if message.topic() == KafkaTopic.PREDICTION:
                        self._handle_prediction(Prediction.model_validate_json(message.value()))
                    else:
                        self._handle_request(ControlRequest.model_validate_json(message.value()))
                except ValidationError:
                    LOG.exception(
                        "Skipped invalid control event topic=%s partition=%s offset=%s",
                        message.topic(),
                        message.partition(),
                        message.offset(),
                    )
                self.consumer.commit(message=message, asynchronous=False)
        finally:
            self.consumer.close()
            self.client.disconnect()
            self.client.loop_stop()

    def stop(self, *_args) -> None:
        self.stop_event.set()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_metrics_server()
    controller = SafetyController()
    signal.signal(signal.SIGTERM, controller.stop)
    signal.signal(signal.SIGINT, controller.stop)
    controller.run()


if __name__ == "__main__":
    main()
