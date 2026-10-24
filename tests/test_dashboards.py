import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = ROOT / "infra" / "grafana" / "dashboards"


def load_builder():
    spec = importlib.util.spec_from_file_location(
        "build_dashboards", ROOT / "infra" / "grafana" / "build_dashboards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()
BOARDS = {
    "pipeline-operations.json": BUILDER.operations,
    "gas-monitoring.json": BUILDER.gas,
}


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_committed_dashboard_matches_builder(name: str) -> None:
    committed = json.loads((DASHBOARDS / name).read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(BOARDS[name](), ensure_ascii=False)), (
        f"{name} is stale: run `uv run python infra/grafana/build_dashboards.py`"
    )


@pytest.mark.parametrize("name", sorted(BOARDS))
def test_panels_have_unique_ids_refids_and_fit_the_grid(name: str) -> None:
    board = BOARDS[name]()
    ids = [panel["id"] for panel in board["panels"]]
    assert len(ids) == len(set(ids))
    for panel in board["panels"]:
        grid = panel["gridPos"]
        assert grid["x"] + grid["w"] <= 24, panel["title"]
        ref_ids = [target["refId"] for target in panel.get("targets", [])]
        assert len(ref_ids) == len(set(ref_ids)), f"duplicate refId in {panel['title']!r}"


def test_pipeline_map_connects_every_stage() -> None:
    canvas = next(panel for panel in BUILDER.operations()["panels"] if panel["type"] == "canvas")
    elements = {element["name"]: element for element in canvas["options"]["root"]["elements"]}
    fields = {target["legendFormat"] for target in canvas["targets"]}
    for element in elements.values():
        for connection in element.get("connections", []):
            assert connection["targetName"] in elements
        text = element["config"].get("text", {})
        if text.get("mode") == "field":
            assert text["field"] in fields
