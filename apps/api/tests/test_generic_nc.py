import hashlib
import io
import json
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from app import main
from app.generic_nc import build_programs, number, turning_lines
from app.toolpath_ir import ToolpathChannel, ToolpathCommand, ToolpathProgram, ToolpathTrace
from test_l32_operation_trial import job, operation, rotational, snapshot
from test_l32_indexed_pocket import pocket_analysis


def complete_planning(source, directory, status="validated_draft"):
    """Install a successful finalization fixture bound to the current source and accepted prefix."""
    from app.agent.l32_operation_trial import evidence_context_signature, operation_signature
    geometry, machine = rotational(), snapshot()
    context = evidence_context_signature(source, geometry, machine)
    main.write_json(directory / "rotational-features.json", geometry.model_dump(mode="json"))
    main.write_json(directory / "machine-configuration.json", machine.model_dump(mode="json"))
    operations = [op for setup in source.plan.setups for op in setup.operations if op.enabled]
    main.write_json(directory / "harness-l32-operation-state.json", {"context_signature": context, "accepted": [
        {"operation_id": op.id, "operation_signature": operation_signature(op), "cumulative_simulation": {}}
        for op in operations]})
    main.write_json(directory / "harness-l32-plan-finalization.json", {
        "status": status, "completion_allowed": True, "context_signature": context,
        "nc_source_signature": main._generic_nc_source(source.id)[2],
        "accepted_operation_ids": [op.id for op in operations], "whole_plan_validation": {"status": "passed"}})


def test_turning_uses_diameter_coordinates_and_preserves_plan():
    source = job(operation("OP10", 10, "turn_od_finishing"))
    before = source.model_dump_json()
    manifest, files = build_programs(source, rotational().model_dump(mode="json"))
    nc = next(iter(files.values()))
    assert manifest["status"] == "generated"
    assert manifest["production_ready"] is False
    assert "G95 F0.08" in nc and "T0101" in nc
    assert "G01 X12.8 Z-30" in nc  # Existing provider compensates radius 6 by a 0.4 nose; no double scaling/compensation.
    assert "G40" in nc and "G41" not in nc and "G42" not in nc
    assert "G92 S8000\nG96 S100" in nc
    assert nc.startswith("%\nO1001") and nc.endswith("M30\n%\n")
    assert "NOT FOR MACHINE USE" in nc and "CMDTYP 4" in nc
    assert source.model_dump_json() == before


def test_partial_generation_reports_unsupported_and_excludes_disabled():
    enabled = operation("OP10", 10, "turn_facing")
    unsupported = operation("OP20", 20, "unimplemented_transfer")
    disabled = operation("OP30", 30, "turn_facing").model_copy(update={"enabled": False})
    manifest, files = build_programs(job(enabled, unsupported, disabled), None)
    assert manifest["status"] == "partial"
    assert manifest["operation_count"] == 2 and len(files) == 1
    assert manifest["missing"][0]["operation_id"] == "OP20"


def test_sub_channel_and_milling_coordinates_are_separate():
    turn = operation("OP10", 10, "turn_facing").model_copy(update={"channel_id": "sub", "spindle_id": "sub"})
    pocket = operation("OP20", 20, "turn_facing")
    pocket.type = "pocket_roughing"
    pocket.feature_ids = ["MF-1"]
    pocket.tool = pocket.tool.model_copy(update={"diameter_mm": 1})
    pocket.parameters = {"spindle_rpm": 2000, "feed_rate_mm_min": 123, "step_down_mm": 0.3}
    source = job(turn, pocket)
    source.analysis = pocket_analysis()
    manifest, files = build_programs(source, None)
    assert manifest["status"] == "generated"
    assert manifest["programs"][0]["channel"] == "sub"
    assert manifest["programs"][1]["coordinate_frame"] == "SOURCE_STEP_XYZ_MM"
    nc = files[manifest["programs"][1]["filename"]]
    assert "G17" in nc and "G94 F123" in nc
    assert "G01 X-3.25" in nc  # Cartesian source X is never doubled.


def test_thread_passes_retract_and_return_to_same_start_before_next_pass():
    trace = ToolpathTrace(source="test", generator_version="1", feature_ids=[])
    commands = [ToolpathCommand(sequence=1, type="rapid_move", operation_id="OP10", channel_id="main", axes={"X": 24, "Z": 2}, trace=trace)]
    for i, target in enumerate((19.5, 19), 2):
        commands.append(ToolpathCommand(sequence=i, type="thread_cut", operation_id="OP10", channel_id="main", trace=trace,
            parameters={"start_z_mm": 0, "end_z_mm": -10, "pitch_mm": 1, "major_diameter_mm": 20,
                        "minor_diameter_mm": 19, "pass_count": 2, "target_diameter_mm": target}))
    program = ToolpathProgram(coordinate_convention="diameter-x_z", machine_snapshot_hash="a"*64, plan_revision=1,
                             channels=[ToolpathChannel(id="main", commands=commands)])
    lines = turning_lines(program, {})
    assert lines.count("G33 Z-10 F1") == 2
    assert "\n".join(lines).count("G33 Z-10 F1\nG00 X24\nG00 Z0") == 2


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, "1"])
def test_invalid_numeric_values_are_rejected(value):
    with pytest.raises(ValueError): number(value)


def test_api_generation_download_and_stale_detection_do_not_bypass_production_gate(tmp_path, monkeypatch):
    source = job(operation("OP10", 10, "turn_facing"))
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    directory = tmp_path / source.id
    directory.mkdir()
    main.save_job(directory, source)
    # Existing production evidence must remain untouched, even when it blocks NC.
    main.write_json(directory / "verification.json", {"status": "failed"})
    (directory / "program.nc").write_text("PRODUCTION FILE", encoding="utf-8")
    client = TestClient(main.app)
    base = f"/api/v1/jobs/{source.id}/gcode/generic"
    complete_planning(source, directory)
    assert client.get(base).json()["status"] == "not_generated"
    response = client.post(base)
    assert response.status_code == 200
    manifest = response.json()
    program = manifest["programs"][0]
    content = client.get(program["url"])
    assert content.status_code == 200
    assert hashlib.sha256(content.content).hexdigest() == program["sha256"]
    bundle = client.get(manifest["bundle_url"])
    with ZipFile(io.BytesIO(bundle.content)) as archive:
        assert archive.read(program["filename"]) == content.content
        assert json.loads(archive.read("manifest.json"))["production_ready"] is False
        assert "README.txt" in archive.namelist()
    assert client.get(f"/api/v1/jobs/{source.id}/files/program.nc").status_code == 409
    assert (directory / "program.nc").read_text(encoding="utf-8") == "PRODUCTION FILE"
    assert client.get(base + "/files/unknown.nc").status_code == 404
    source.plan.setups[0].operations[0].parameters["feed_per_revolution_mm"] = 0.12
    main.save_job(directory, source)
    assert client.get(base).json()["status"] == "blocked"
    assert client.get(program["url"]).status_code == 409
    assert client.get(base + "/bundle").status_code == 409
    assert client.post(base).status_code == 409
    complete_planning(source, directory)
    regenerated = client.post(base).json()
    assert "G95 F0.12" in client.get(regenerated["programs"][0]["url"]).text


def test_api_detects_plan_change_during_generation(tmp_path, monkeypatch):
    from app import generic_nc
    source = job(operation("OP10", 10, "turn_facing"))
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    directory = tmp_path / source.id
    directory.mkdir()
    main.save_job(directory, source)
    complete_planning(source, directory)
    original = generic_nc.build_programs
    def changing(*args):
        result = original(*args)
        source.plan.setups[0].operations[0].parameters["face_z_mm"] = -1
        main.save_job(directory, source)
        return result
    monkeypatch.setattr(generic_nc, "build_programs", changing)
    response = TestClient(main.app).post(f"/api/v1/jobs/{source.id}/gcode/generic")
    assert response.status_code == 409
    assert not (directory / "generic-nc" / "manifest.json").exists()


@pytest.mark.parametrize("state", ["missing", "blocked", "plan_incomplete", "not_allowed", "stale", "unaccepted", "provisional"])
def test_incomplete_planning_blocks_generation_and_existing_downloads(tmp_path, monkeypatch, state):
    from app import generic_nc
    source = job(operation("OP10", 10, "turn_facing"))
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    directory = tmp_path / source.id
    directory.mkdir()
    main.save_job(directory, source)
    complete_planning(source, directory)
    final_path = directory / "harness-l32-plan-finalization.json"
    final = json.loads(final_path.read_text())
    if state == "missing":
        final_path.unlink()
    elif state in {"blocked", "plan_incomplete"}:
        main.write_json(final_path, {**final, "status": state})
    elif state == "not_allowed":
        main.write_json(final_path, {**final, "completion_allowed": False})
    elif state == "stale":
        main.write_json(final_path, {**final, "nc_source_signature": "old"})
    else:
        accepted_path = directory / "harness-l32-operation-state.json"
        accepted = json.loads(accepted_path.read_text())
        if state == "unaccepted": accepted["accepted"] = []
        else: accepted["accepted"][0]["acceptance_state"] = "provisional"
        main.write_json(accepted_path, accepted)
    archived = directory / "generic-nc"
    archived.mkdir()
    main.write_json(archived / "manifest.json", {"programs": [{"filename": "old.nc"}]})
    (directory / "program.nc").write_text("HISTORICAL PROGRAM")
    def forbidden(*args):
        pytest.fail("Incomplete plans must never invoke NC generation")
    monkeypatch.setattr(generic_nc, "build_programs", forbidden)
    client = TestClient(main.app)
    base = f"/api/v1/jobs/{source.id}/gcode/generic"
    assert client.get(f"/api/v1/jobs/{source.id}/gcode/planning-status").json()["allowed"] is False
    assert client.post(base).status_code == 409
    assert client.get(base).json()["programs"] == []
    assert client.get(base + "/files/old.nc").status_code == 409
    assert client.get(base + "/bundle").status_code == 409
    assert client.get(f"/api/v1/jobs/{source.id}/files/program.nc").status_code == 409
    assert (directory / "program.nc").read_text() == "HISTORICAL PROGRAM"


def test_revoked_finalization_during_generation_does_not_publish_programs(tmp_path, monkeypatch):
    from app import generic_nc
    source = job(operation("OP10", 10, "turn_facing"))
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path)
    directory = tmp_path / source.id
    directory.mkdir()
    main.save_job(directory, source)
    complete_planning(source, directory)
    original = generic_nc.build_programs
    def revoke(*args):
        result = original(*args)
        main.write_json(directory / "harness-l32-plan-finalization.json", {"status": "blocked"})
        return result
    monkeypatch.setattr(generic_nc, "build_programs", revoke)
    response = TestClient(main.app).post(f"/api/v1/jobs/{source.id}/gcode/generic")
    assert response.status_code == 409
    assert not (directory / "generic-nc").exists()
