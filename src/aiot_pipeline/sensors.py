"""Single description of the 16-channel MOX array, shared by the simulator, UI and dashboards."""

from dataclasses import asdict, dataclass

# Column order of the UCI 322 ethylene_CO recording.
SENSOR_TYPES = (
    "TGS2602",
    "TGS2602",
    "TGS2600",
    "TGS2600",
    "TGS2610",
    "TGS2610",
    "TGS2620",
    "TGS2620",
) * 2

SENSOR_TARGETS = {
    "TGS2602": "VOC, mùi, NH₃/H₂S",
    "TGS2600": "Khí ô nhiễm: CO, H₂",
    "TGS2610": "LPG: propan, butan",
    "TGS2620": "Hơi dung môi, cồn, CO",
}

# All 16 channels sit on one e-nose board in the hood plenum, next to the gas line: the model
# needs them to sample the same air at the same moment, as in the UCI 322 test chamber. The
# board has one row per sensor type and four columns.
TYPE_ORDER = ("TGS2602", "TGS2600", "TGS2610", "TGS2620")


def board_position(index: int) -> str:
    sensor_type = SENSOR_TYPES[index]
    column = [i for i, kind in enumerate(SENSOR_TYPES) if kind == sensor_type].index(index) + 1
    return f"E-nose hàng {TYPE_ORDER.index(sensor_type) + 1} · ô {column}"


@dataclass(frozen=True)
class SensorInfo:
    channel: str
    index: int
    sensor_type: str
    target: str
    location: str


SENSORS = tuple(
    SensorInfo(
        channel=f"S{index + 1}",
        index=index,
        sensor_type=sensor_type,
        target=SENSOR_TARGETS[sensor_type],
        location=board_position(index),
    )
    for index, sensor_type in enumerate(SENSOR_TYPES)
)


def sensors_as_dicts() -> list[dict[str, object]]:
    return [asdict(sensor) for sensor in SENSORS]
