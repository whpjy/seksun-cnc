"""Optional real FreeCAD smoke test against an existing STEP job, isolated in /tmp.

Run with CNC_TRIAL_SMOKE_JOB=<job-id>; it never edits the source job.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.agent.operation_trial import run_operation_trial
from app.main import ANALYZER_BIN, CAM_ADAPTER_SCRIPT, FREECAD_CMD, STORAGE_ROOT
from app.models import JobResponse
from app.planner import build_process_plan
from app.qwen import QwenSettings


@pytest.mark.skipif(not os.getenv("CNC_TRIAL_SMOKE_JOB"), reason="set CNC_TRIAL_SMOKE_JOB for real CAM trial")
def test_real_first_operation_trial_does_not_touch_source_job() -> None:
    job_id = os.environ["CNC_TRIAL_SMOKE_JOB"]
    with tempfile.TemporaryDirectory(prefix="cnc-trial-smoke-") as temporary:
        directory = Path(temporary)
        if job_id == "synthetic":
            source_filename = "block.step"
            source_path = directory / source_filename
            code = f"import Part; Part.makeBox(30, 20, 8).exportStep({str(source_path)!r})"
            subprocess.run([FREECAD_CMD, "-c", code], check=True, capture_output=True, text=True)
            analysis_path = directory / "analysis.json"
            subprocess.run([ANALYZER_BIN, str(source_path), str(analysis_path), str(directory / "model.stl")],
                           check=True, capture_output=True, text=True)
            from app.models import GeometryAnalysis
            analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        else:
            assert len(job_id) == 32 and all(character in "0123456789abcdef" for character in job_id)
            source_dir = STORAGE_ROOT / job_id
            job = JobResponse.model_validate_json((source_dir / "job.json").read_text(encoding="utf-8"))
            assert job.analysis is not None
            if job.device_id != "VMC-850" or job.plan is None:
                pytest.skip("stored job is not a VMC-850 FreeCAD CAM plan; do not substitute a different machine plan")
            source_filename = job.filename
            analysis = job.analysis
            source_path = source_dir / source_filename
            assert source_path.is_file()
            shutil.copy2(source_path, directory / source_filename)
            shutil.copy2(source_dir / "model.stl", directory / "model.stl")
        plan = build_process_plan(analysis, "6061-T6", "VMC-850") if job_id == "synthetic" else job.plan
        first = next(operation for setup in plan.setups for operation in setup.operations if operation.enabled)
        print({"setup_axis": plan.setups[0].work_axis.model_dump(), "type": first.type,
               "tool_diameter": first.tool.diameter_mm, "parameters": first.parameters})
        result = run_operation_trial(
            directory=directory, source_filename=source_filename, analysis=analysis,
            plan=plan, operation_id=first.id, freecad_command=FREECAD_CMD,
            adapter_script=CAM_ADAPTER_SCRIPT, open_questions=[],
            qwen_settings=QwenSettings(api_key="", base_url="", model="disabled", timeout_seconds=1),
        )
        print({
            "operation_id": result["operation_id"], "status": result["status"],
            "cut_segment_count": result["evidence"].get("cut_segment_count"),
            "cam_error": str(result["evidence"].get("cam_error", ""))[:900],
        })
        assert result["operation_id"] == first.id
        if job_id == "synthetic":
            assert first.tool.diameter_mm == 10.0
            assert result["evidence"]["cut_segment_count"] > 0
        assert result["status"] in {"candidate", "needs_review", "blocked"}
        if result["status"] == "blocked":
            assert result["evidence"].get("cam_error") or result["evidence"]["cut_segment_count"] >= 0
        else:
            assert result["evidence"]["generated_operation_ids"] == [first.id]
        assert result["production_ready"] is False
