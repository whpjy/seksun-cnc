from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from PIL import Image
from fastapi import HTTPException

from app.agent.operation_trial import (
    isolate_operation,
    render_material_evidence,
    review_operation_trial,
    run_operation_trial,
)
from app.planner import build_process_plan
from app.qwen import QwenPlanningError, QwenSettings
from test_planner import sample_analysis


def test_isolated_trial_contains_only_selected_operation_without_mutating_plan() -> None:
    original = build_process_plan(sample_analysis(), "6061-T6", "VMC-850")
    selected = original.setups[0].operations[0].id

    isolated = isolate_operation(original, selected)

    assert [operation.id for setup in isolated.setups for operation in setup.operations] == [selected]
    assert isolated.stock == original.stock
    assert isolated.setups[0].fixture == original.setups[0].fixture
    assert len(original.setups[0].operations) == 2
    with pytest.raises(ValueError):
        isolate_operation(original, "MISSING")


def test_material_evidence_uses_simulator_height_field(tmp_path: Path) -> None:
    path = render_material_evidence({
        "method": "cumulative_double_sided_height_field",
        "surface": {
            "columns": 2, "rows": 2, "bottom_z": 0, "top_z": 10,
            "heights": [10, 5, 0, 10], "lower_heights": [0, 0, 0, 0],
        },
    }, tmp_path / "stock.png")

    with Image.open(path) as image:
        assert image.size == (680, 690)
        assert image.getpixel((100, 100)) != image.getpixel((500, 100))
    with pytest.raises(ValueError):
        render_material_evidence({"surface": {"columns": 2, "rows": 2}}, tmp_path / "bad.png")


def test_qwen_review_cannot_approve_different_operation(tmp_path: Path) -> None:
    image = tmp_path / "view.png"
    Image.new("RGB", (2, 2), "white").save(image)
    settings = QwenSettings(
        api_key="test-key", base_url="https://example.test/v1",
        model="test-model", timeout_seconds=3,
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "schema_version": "1.0.0", "operation_id": "OTHER", "verdict": "promising",
            "confidence": 0.8, "reasoning": "test", "observed_findings": [],
            "risks": [], "missing_evidence": [], "suggested_changes": [],
        })}}]})

    with pytest.raises(QwenPlanningError, match="编号"):
        review_operation_trial(
            "OP10", {"cut_segment_count": 3}, image, image,
            settings=settings, transport=httpx.MockTransport(handler),
        )
    assert len(requests[0]["messages"][1]["content"]) == 3
    assert all(item["type"] == "image_url" for item in requests[0]["messages"][1]["content"][1:])


@pytest.mark.parametrize("questions, expected", [([], "candidate"), (["装夹夹具未确认"], "needs_review")])
def test_trial_runs_one_operation_and_never_releases_nc(tmp_path: Path, monkeypatch, questions, expected) -> None:
    import app.agent.operation_trial as module

    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    operation_id = plan.setups[0].operations[0].id
    (tmp_path / "part.step").write_text("test", encoding="utf-8")
    Image.new("RGB", (2, 2), "white").save(tmp_path / "agent-perception-contact-sheet.png")

    def fake_cam(_command, _script, arguments, *, trial_mode=False):
        assert trial_mode is True
        isolated = json.loads(arguments[2].read_text(encoding="utf-8"))
        assert [item["id"] for setup in isolated["setups"] for item in setup["operations"]] == [operation_id]
        arguments[3].write_text("fcstd", encoding="utf-8")
        arguments[4].write_text("draft nc", encoding="utf-8")
        arguments[5].write_text(json.dumps({
            "generated_operations": [operation_id],
            "preview_segments": [{"operation_id": operation_id, "motion": "cut"}],
        }), encoding="utf-8")

    monkeypatch.setattr(module, "run_freecad_adapter", fake_cam)
    monkeypatch.setattr(module, "verify_cam", lambda *_: {"checks": [
        {"id": check, "status": "passed"} for check in (
            "machine_travel", "cutting_parameters", "tool_diameter", "through_hole_depth", "operation_coverage",
        )
    ], "warnings": []})
    monkeypatch.setattr(module, "simulate_material_removal", lambda *_: {
        "status": "completed", "method": "height_field", "warnings": [],
        "operation_snapshots": [{"operation_id": operation_id, "status": "completed", "removed_volume_delta_mm3": 3}],
        "surface": {"columns": 2, "rows": 2, "bottom_z": 0, "top_z": 10,
                    "heights": [10, 8, 8, 10], "lower_heights": [0, 0, 0, 0]},
    })
    monkeypatch.setattr(module, "detect_collisions", lambda *_: {
        "status": "passed", "collisions": [], "low_rapids": [],
    })
    monkeypatch.setattr(module, "review_operation_trial", lambda *_args, **_kwargs: {
        "review": {"verdict": "promising", "confidence": 0.8, "missing_evidence": [], "reasoning": "仅作为候选"},
    })

    trial = run_operation_trial(
        directory=tmp_path, source_filename="part.step", analysis=analysis,
        plan=plan, operation_id=operation_id, freecad_command="fake-freecad",
        adapter_script=Path("adapter.py"), open_questions=questions,
    )

    assert trial["status"] == expected
    assert trial["production_ready"] is False
    assert "program.nc" not in trial["files"]
    assert (tmp_path / "agent-trials" / trial["trial_id"] / "program.nc").is_file()
    assert (tmp_path / "agent-operation-trial.json").is_file()


def test_failed_cam_is_archived_as_blocked_evidence(tmp_path: Path, monkeypatch) -> None:
    import app.agent.operation_trial as module

    analysis = sample_analysis()
    plan = build_process_plan(analysis, "6061-T6", "VMC-850")
    operation_id = plan.setups[0].operations[0].id

    def reject_cam(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "FreeCADCmd", stderr="unsupported native operation")

    monkeypatch.setattr(module, "run_freecad_adapter", reject_cam)
    monkeypatch.setattr(module, "review_operation_trial", lambda *_args, **_kwargs: (_ for _ in ()).throw(QwenPlanningError("disabled in test")))
    trial = run_operation_trial(
        directory=tmp_path, source_filename="part.step", analysis=analysis,
        plan=plan, operation_id=operation_id, freecad_command="fake-freecad",
        adapter_script=Path("adapter.py"), open_questions=[],
    )

    assert trial["status"] == "blocked"
    assert trial["ai"]["status"] == "unavailable"
    assert "unsupported native operation" in trial["evidence"]["cam_error"]
    assert (tmp_path / "agent-operation-trial.json").is_file()


def test_trial_file_route_never_serves_trial_nc(tmp_path: Path, monkeypatch) -> None:
    import app.main as main

    trial_dir = tmp_path / "agent-trials" / "OP10-deadbeef"
    trial_dir.mkdir(parents=True)
    (trial_dir / "program.nc").write_text("G1 X1", encoding="utf-8")
    (trial_dir / "result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(main, "load_job", lambda _job_id: None)
    monkeypatch.setattr(main, "job_directory", lambda _job_id: tmp_path)

    assert Path(main.get_operation_trial_file("test", "OP10-deadbeef", "result.json").path) == trial_dir / "result.json"
    with pytest.raises(HTTPException) as denied:
        main.get_operation_trial_file("test", "OP10-deadbeef", "program.nc")
    assert denied.value.status_code == 404
