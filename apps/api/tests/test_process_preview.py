import json

import pytest
from fastapi import HTTPException
from app import main
from app.process_preview import operation_preview
from app.process_preview import cumulative_playback
from app.agent.l32_operation_trial import accept_trial, run_independent_trial
from test_l32_operation_trial import job, operation, rotational, snapshot


def prepare(monkeypatch, tmp_path, blocked=False):
    first = operation("OP10", 10, "turn_facing")
    if blocked:
        first.parameters["face_z_mm"] = -10
    source = job(first, operation("OP20", 20, "turn_od_finishing"))
    geometry, machine = rotational(), snapshot()
    monkeypatch.setattr(main, "load_job", lambda _: source)
    monkeypatch.setattr(main, "job_directory", lambda _: tmp_path)
    monkeypatch.setattr(main, "load_machine_snapshot", lambda _: machine)
    (tmp_path / "rotational-features.json").write_text(geometry.model_dump_json())
    (tmp_path / "machine-configuration.json").write_text("{}")
    trial, state = run_independent_trial(job=source, operation_id="OP10", rotational=geometry,
                                         snapshot=machine, state=None, resolution_mm=0.5)
    (tmp_path / "agent-l32-operation-trial.json").write_text(json.dumps(trial))
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(state))
    return source, trial, state


def test_preview_contains_real_commands_and_initial_material(monkeypatch, tmp_path):
    source, trial, _ = prepare(monkeypatch, tmp_path)
    preview = operation_preview(source.id, "OP10")
    assert preview["toolpath"] == trial["toolpath"]
    assert preview["coordinate_frame"] == "source_model"
    assert preview["source_cutoff_z"] is None
    assert preview["after_samples"] == trial["cumulative_simulation"]["samples"]
    assert all(sample["outer_radius"] == 12 for sample in preview["before_samples"])
    assert preview["revision"] == operation_preview(source.id, "OP10")["revision"]


def test_blocked_trial_is_visible_as_failed_evidence(monkeypatch, tmp_path):
    source, _, _ = prepare(monkeypatch, tmp_path, blocked=True)
    preview = operation_preview(source.id, "OP10")
    assert preview["status"] == "blocked"
    assert preview["reason"] == "profile_overcut"
    assert preview["can_accept"] is False
    assert preview["after_samples"] and preview["toolpath"]


def test_previous_accepted_stock_is_used_and_archived_trial_remains_readable(monkeypatch, tmp_path):
    source, first, state = prepare(monkeypatch, tmp_path)
    accepted = accept_trial(job=source, state=state, trial=first, operation_id="OP10")
    second, _ = run_independent_trial(job=source, operation_id="OP20", rotational=rotational(),
                                     snapshot=snapshot(), state=accepted, resolution_mm=0.5)
    (tmp_path / "agent-l32-trial-OP10.json").write_text(json.dumps(first))
    (tmp_path / "agent-l32-operation-trial.json").write_text(json.dumps(second))
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(accepted))
    assert operation_preview(source.id, "OP20")["before_samples"] == first["cumulative_simulation"]["samples"]
    assert operation_preview(source.id, "OP10")["toolpath"] == first["toolpath"]


@pytest.mark.parametrize("change", ["operation", "stock", "prefix"])
def test_changed_evidence_is_rejected(monkeypatch, tmp_path, change):
    source, trial, _ = prepare(monkeypatch, tmp_path)
    if change == "operation":
        source.plan.setups[0].operations[0].parameters["face_z_mm"] = -1
    elif change == "stock":
        source.plan.stock["diameter_mm"] = 26
    else:
        trial["accepted_prefix_signatures"] = ["obsolete"]
        (tmp_path / "agent-l32-operation-trial.json").write_text(json.dumps(trial))
    with pytest.raises(HTTPException) as error:
        operation_preview(source.id, "OP10")
    assert error.value.status_code == 409


def test_missing_or_invalid_operation_never_returns_another_operations_trial(monkeypatch, tmp_path):
    source, _, _ = prepare(monkeypatch, tmp_path)
    for target in ["OP20", "../OP10", "OP404"]:
        with pytest.raises(HTTPException) as error:
            operation_preview(source.id, target)
        assert error.value.status_code == 404


def test_non_l32_job_is_rejected(monkeypatch, tmp_path):
    source, _, _ = prepare(monkeypatch, tmp_path)
    source.device_id = "seksun-freecad-cam-standard"
    with pytest.raises(HTTPException) as error:
        operation_preview(source.id, "OP10")
    assert error.value.status_code == 409


def test_recorded_cut_states_finish_at_actual_solver_stock(monkeypatch, tmp_path):
    source, trial, _ = prepare(monkeypatch, tmp_path)
    preview = operation_preview(source.id, "OP10")
    samples = [dict(sample) for sample in preview["before_samples"]]
    commands = [command for channel in preview["toolpath"]["channels"] for command in channel["commands"]]
    previous_position = (-1, 0)
    for frame in preview["material_frames"]:
        assert (frame["command_index"], frame["fraction"]) >= previous_position
        previous_position = frame["command_index"], frame["fraction"]
        if frame["changes"]:
            assert commands[frame["command_index"]]["type"] != "rapid_move"
        for index, outer, inner in frame["changes"]:
            assert outer <= samples[index]["outer_radius"]
            assert inner >= samples[index]["inner_radius"]
            samples[index].update(outer_radius=outer, inner_radius=inner)
    assert samples == preview["after_samples"]
    assert cumulative_playback(source.id, "OP10")["complete"] is True


def test_cumulative_playback_stitches_accepted_prefix_and_stops_at_missing_record(monkeypatch, tmp_path):
    source, first, state = prepare(monkeypatch, tmp_path)
    accepted = accept_trial(job=source, state=state, trial=first, operation_id="OP10")
    second, _ = run_independent_trial(job=source, operation_id="OP20", rotational=rotational(),
                                     snapshot=snapshot(), state=accepted, resolution_mm=0.5)
    (tmp_path / "agent-l32-trial-OP10.json").write_text(json.dumps(first))
    (tmp_path / "agent-l32-operation-trial.json").write_text(json.dumps(second))
    (tmp_path / "harness-l32-operation-state.json").write_text(json.dumps(accepted))
    chain = cumulative_playback(source.id, "OP20")
    assert chain["complete"] is True
    assert [item["operation_id"] for item in chain["previews"]] == ["OP10", "OP20"]
    assert chain["previews"][0]["after_samples"] == chain["previews"][1]["before_samples"]
    (tmp_path / "agent-l32-trial-OP10.json").unlink()
    chain = cumulative_playback(source.id, "OP20")
    assert chain["complete"] is False
    assert chain["missing_operation_id"] == "OP10"


def test_legacy_trial_replays_solver_and_refuses_changed_final_stock(monkeypatch, tmp_path):
    source, trial, _ = prepare(monkeypatch, tmp_path)
    trial["cumulative_simulation"]["material_frames"] = []
    path = tmp_path / "agent-l32-operation-trial.json"
    path.write_text(json.dumps(trial))
    assert operation_preview(source.id, "OP10")["material_frames"]
    trial["cumulative_simulation"]["samples"][0]["outer_radius"] += 1
    path.write_text(json.dumps(trial))
    with pytest.raises(HTTPException) as error:
        operation_preview(source.id, "OP10")
    assert error.value.status_code == 409
