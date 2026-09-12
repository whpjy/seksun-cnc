from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .models import (
    FeatureReviewRequest, GeometryAnalysis, JobResponse, OperationCreateRequest,
    OperationReorderRequest, OperationUpdateRequest, ProcessPlan, SafetyConfigurationRequest,
)
from .planner import build_process_plan
from .catalogs import apply_cutting_parameters, catalog_payload, get_tool, resolve_machine, resolve_material
from .operation_library import (
    create_operation_instance, get_operation_definition, operation_library_payload, validate_parameters,
)
from .collision import build_safety_configuration, detect_collisions
from .conformance import compare_stock_to_target_mesh
from .preflight import verify_cam
from .simulation import simulate_material_removal
from .recognizer import normalize_manufacturing_features
from .engines import probe_engine, resolve_executable, run_camotics, run_freecad_adapter
from .forming import build_forming_preview
from .qwen import QwenPlanningError, probe_qwen, qwen_config_payload, review_process_plan
from .benchmarks import example_catalog_payload
from .coverage import evaluate_plan_coverage


APP_ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = Path(os.getenv("CNC_STORAGE_ROOT", APP_ROOT / ".seksun-cnc" / "jobs"))
ANALYZER_BIN = os.getenv("CNC_ANALYZER_BIN", "occt-analyzer")
FREECAD_CMD = os.getenv("CNC_FREECAD_CMD", "FreeCADCmd")
CAMOTICS_CMD = os.getenv("CNC_CAMOTICS_CMD", "camsim")
CAM_ADAPTER_SCRIPT = Path(os.getenv("CNC_CAM_ADAPTER_SCRIPT", APP_ROOT / "cam" / "freecad_adapter.py"))
MAX_UPLOAD_BYTES = int(os.getenv("CNC_MAX_UPLOAD_MB", "200")) * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("CNC_PUBLIC_BASE_URL", "").rstrip("/")
BENCHMARK_REPORT_PATH = Path(os.getenv(
    "CNC_BENCHMARK_REPORT", STORAGE_ROOT.parent / "benchmark-report.json",
))
CAM_STREAM_LOCK = threading.Lock()
CAM_STREAMING_JOBS: set[str] = set()
AI_REVIEW_LOCK = threading.Lock()
AI_REVIEWING_JOBS: set[str] = set()

@asynccontextmanager
async def lifespan(_: FastAPI):
    STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Seksun CNC API", version="0.8.0", lifespan=lifespan)
origins = [item.strip() for item in os.getenv("CNC_CORS_ORIGINS", "http://localhost:5174").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def volume_conformance(
    target_volume: float,
    remaining_volume: float,
    initial_stock_volume: float,
) -> tuple[str, float, float]:
    """Grade final stock against the target part, not the oversized blank."""
    absolute_error = abs(remaining_volume - target_volume)
    target_error = absolute_error / max(target_volume, 1e-6) * 100
    stock_error = absolute_error / max(initial_stock_volume, 1e-6) * 100
    if target_error <= 8:
        status = "passed"
    elif target_error <= 15:
        status = "warning"
    else:
        status = "failed"
    return status, target_error, stock_error


def compact_preview_segments(segments: list[dict], maximum: int = 45_000) -> list[dict]:
    """Bound browser preview size while preserving the production toolpath.

    Simulation, collision checks and NC export use the complete segment list;
    this is called only after those stages have finished.
    """
    if len(segments) <= maximum:
        return segments
    stride = max(2, (len(segments) + maximum - 1) // maximum)
    selected = set(range(0, len(segments), stride))
    selected.add(len(segments) - 1)
    for index in range(1, len(segments)):
        if segments[index].get("operation_id") != segments[index - 1].get("operation_id"):
            selected.update((index - 1, index))
    return [segments[index] for index in sorted(selected)]


def job_directory(job_id: str) -> Path:
    if not job_id or any(character not in "0123456789abcdef" for character in job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return STORAGE_ROOT / job_id


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_camotics_project(
    directory: Path,
    setup_plan: object,
    setup_result: dict[str, object],
    program_name: str,
    resolution_mm: float,
) -> Path:
    """Describe one setup explicitly instead of asking CAMotics to guess it."""
    generated = set(setup_result.get("generated_operations", []))
    operations = [operation for operation in setup_plan.operations if operation.id in generated]
    tools: dict[str, object] = {}
    for operation in operations:
        number = max(int(operation.sequence // 10), 1)
        tools[str(number)] = {
            "units": "metric",
            "shape": "conical" if operation.tool.kind == "chamfer_mill" else "cylindrical",
            "length": max(float(operation.tool.flute_length_mm), 0.1),
            "diameter": max(float(operation.tool.diameter_mm), 0.1),
            "description": operation.tool.name,
        }
    bounds = setup_result.get("local_bounds")
    if not isinstance(bounds, dict):
        raise ValueError("FreeCAD setup result did not include local stock bounds")
    minimum, maximum = bounds.get("minimum"), bounds.get("maximum")
    if not isinstance(minimum, dict) or not isinstance(maximum, dict):
        raise ValueError("FreeCAD setup result contained invalid local stock bounds")
    project = {
        "units": "metric",
        "resolution-mode": "manual",
        "resolution": resolution_mm,
        "tools": tools,
        "workpiece": {
            "automatic": False,
            "margin": 0,
            "bounds": {
                "min": [float(minimum[axis]) for axis in "xyz"],
                "max": [float(maximum[axis]) for axis in "xyz"],
            },
        },
        "files": [program_name],
    }
    safe_setup_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in str(setup_result.get("setup_id", "setup"))
    )
    project_path = directory / f"{safe_setup_id}.camotics"
    write_json(project_path, project)
    return project_path


def load_job(job_id: str) -> JobResponse:
    metadata_path = job_directory(job_id) / "job.json"
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.model_validate_json(metadata_path.read_text(encoding="utf-8"))


def save_job(directory: Path, job: JobResponse) -> None:
    write_json(directory / "job.json", job.model_dump(mode="json"))


def invalidate_cam_artifacts(directory: Path) -> None:
    for filename in (
        "cam.FCStd", "program.nc", "toolpath.json", "verification.json",
        "simulation.json", "collision.json", "ai-plan.json",
    ):
        (directory / filename).unlink(missing_ok=True)
    for pattern in ("program-*.nc", "camotics-*.stl", "*.camotics"):
        for artifact in directory.glob(pattern):
            if artifact.is_file() and artifact.parent == directory:
                artifact.unlink(missing_ok=True)


def cam_response(
    job_id: str,
    result: dict[str, object],
    verification: dict[str, object],
    simulation: dict[str, object],
    collision: dict[str, object],
    stdout: str = "",
) -> dict[str, object]:
    return {
        "status": "completed",
        **result,
        "files": {
            "freecad": f"/api/v1/jobs/{job_id}/files/cam.FCStd",
            "gcode": f"/api/v1/jobs/{job_id}/files/program.nc",
            "preview": f"/api/v1/jobs/{job_id}/files/toolpath.json",
            "verification": f"/api/v1/jobs/{job_id}/files/verification.json",
            "simulation": f"/api/v1/jobs/{job_id}/files/simulation.json",
            "collision": f"/api/v1/jobs/{job_id}/files/collision.json",
            "camotics": [
                f"/api/v1/jobs/{job_id}/files/{surface['file']}"
                for surface in result.get("camotics_surfaces", [])
                if isinstance(surface, dict) and isinstance(surface.get("file"), str)
            ],
        },
        "verification": verification,
        "simulation": simulation,
        "collision": collision,
        "stdout": stdout,
        "safety": (
            "Concept forming preview only. No production NC is generated; unfolding, tooling and forming physics require engineer validation."
            if result.get("process_kind") == "sheet_forming"
            else "Draft toolpaths only. Collision checking uses conservative envelopes and still requires engineer review before machine use."
        ),
    }


@app.get("/health")
def health() -> dict[str, object]:
    freecad = probe_engine("freecad-cam", "toolpath_generation", FREECAD_CMD, ("--version",))
    camotics = probe_engine("camotics", "material_removal", CAMOTICS_CMD, ("--version",))
    return {
        "status": "ok",
        "version": app.version,
        "analyzer": shutil.which(ANALYZER_BIN) or (ANALYZER_BIN if Path(ANALYZER_BIN).is_file() else None),
        "freecad": freecad.executable,
        "cam_status": "available" if freecad.available else "adapter_ready_runtime_missing",
        "verification": "static_preflight_available",
        "simulation": "camotics_available" if camotics.available else "height_field_fallback",
        "collision": "tool_holder_fixture_envelope_available",
        "engines": [freecad.as_dict(), camotics.as_dict()],
    }


@app.get("/api/v1/catalogs")
def get_catalogs() -> dict[str, object]:
    return catalog_payload()


@app.get("/api/v1/operation-library")
def get_operation_library() -> dict[str, object]:
    return operation_library_payload()


@app.get("/api/v1/operation-library/{definition_id}")
def get_operation_library_item(definition_id: str) -> dict[str, object]:
    try:
        return get_operation_definition(definition_id).model_dump(mode="json")
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/v1/config")
def get_config() -> dict[str, object]:
    return {
        "public_base_url": PUBLIC_BASE_URL,
        "session_sharing": True,
        "cam_engine": "freecad-cam",
        "simulation_engine": "camotics-with-heightfield-fallback",
        "ai": qwen_config_payload(),
    }


@app.post("/api/v1/ai/qwen/test")
def test_qwen_connection() -> dict[str, object]:
    """Make one minimal, non-thinking request to verify Qwen credentials."""
    return probe_qwen()


def _optional_job_json(directory: Path, filename: str) -> dict[str, object] | None:
    path = directory / filename
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _ai_review_block_reason(directory: Path) -> str | None:
    payload = _optional_job_json(directory, "ai-plan.json")
    review = payload.get("review") if payload else None
    if not isinstance(review, dict) or review.get("approval_blocked") is not True:
        return None
    summary = review.get("summary")
    return str(summary)[:500] if summary else "Qwen 工艺审查识别到未解决的制造风险"


@app.get("/api/v1/jobs/{job_id}/ai/plan")
def get_ai_plan_review(job_id: str) -> dict[str, object]:
    path = job_directory(job_id) / "ai-plan.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="AI process review is not available")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="AI process review is invalid") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="AI process review is invalid")
    return payload


@app.get("/api/v1/benchmarks/cases")
def get_benchmark_cases() -> dict[str, object]:
    """List the read-only paired 2D/3D examples mounted for regression tests."""
    return example_catalog_payload()


@app.get("/api/v1/benchmarks/report")
def get_benchmark_report() -> dict[str, object]:
    if not BENCHMARK_REPORT_PATH.is_file():
        raise HTTPException(status_code=404, detail="Benchmark report is not available")
    try:
        payload = json.loads(BENCHMARK_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Benchmark report is invalid") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Benchmark report is invalid")
    return payload


@app.post("/api/v1/jobs/{job_id}/ai/plan")
def create_ai_plan_review(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    if not job.analysis or not job.plan:
        raise HTTPException(
            status_code=409,
            detail="Geometry analysis and deterministic process plan are required",
        )
    directory = job_directory(job_id)
    with AI_REVIEW_LOCK:
        if job_id in AI_REVIEWING_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在执行 AI 工艺审查")
        AI_REVIEWING_JOBS.add(job_id)
    artifacts = {
        name: payload
        for name, filename in (
            ("verification", "verification.json"),
            ("collision", "collision.json"),
            ("simulation", "simulation.json"),
        )
        if (payload := _optional_job_json(directory, filename)) is not None
    }
    try:
        try:
            result = review_process_plan(job.analysis, job.plan, artifacts)
        except QwenPlanningError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        with AI_REVIEW_LOCK:
            AI_REVIEWING_JOBS.discard(job_id)
    write_json(directory / "ai-plan.json", result)
    return result


@app.post("/api/v1/jobs", response_model=JobResponse)
async def create_job(
    step: UploadFile = File(...),
    material: str = Form("6061-T6 铝合金"),
    machine: str = Form("VMC850 三轴立式加工中心（FANUC 0i-MF Plus）"),
) -> JobResponse:
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")

    job_id = uuid.uuid4().hex
    directory = job_directory(job_id)
    directory.mkdir(parents=True)
    source_path = directory / filename
    size = 0
    with source_path.open("wb") as output:
        while chunk := await step.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(status_code=413, detail="STEP file exceeds upload limit")
            output.write(chunk)

    job = JobResponse(
        id=job_id,
        status="processing",
        filename=filename,
        created_at=utc_now(),
        material=material,
        machine=machine,
    )
    save_job(directory, job)

    analysis_path = directory / "analysis.json"
    model_path = directory / "model.stl"
    try:
        subprocess.run(
            [ANALYZER_BIN, str(source_path), str(analysis_path), str(model_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        analysis = normalize_manufacturing_features(analysis)
        write_json(analysis_path, analysis.model_dump(mode="json"))
        plan = build_process_plan(analysis, material=material, machine=machine)
        write_json(directory / "plan.json", plan.model_dump(mode="json"))
        job.status = "completed"
        job.analysis = analysis
        job.plan = plan
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        job.status = "failed"
        details = getattr(error, "stderr", None) or str(error)
        job.error = details[-2000:]
    save_job(directory, job)
    return job


@app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return load_job(job_id)


@app.post("/api/v1/jobs/{job_id}/reanalyze", response_model=JobResponse)
def reanalyze_job(job_id: str) -> JobResponse:
    job = load_job(job_id)
    directory = job_directory(job_id)
    source_path = directory / job.filename
    if not source_path.is_file():
        raise HTTPException(status_code=409, detail="Original STEP file is not available")
    invalidate_cam_artifacts(directory)
    job.status = "processing"
    job.error = None
    save_job(directory, job)
    analysis_path = directory / "analysis.json"
    model_path = directory / "model.stl"
    try:
        subprocess.run(
            [ANALYZER_BIN, str(source_path), str(analysis_path), str(model_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
        analysis = normalize_manufacturing_features(analysis)
        write_json(analysis_path, analysis.model_dump(mode="json"))
        plan = build_process_plan(analysis, material=job.material, machine=job.machine)
        write_json(directory / "plan.json", plan.model_dump(mode="json"))
        job.status = "completed"
        job.analysis = analysis
        job.plan = plan
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        job.status = "failed"
        details = getattr(error, "stderr", None) or str(error)
        job.error = details[-2000:]
    save_job(directory, job)
    return job


@app.get("/api/v1/jobs/{job_id}/files/{filename}")
def get_job_file(job_id: str, filename: str) -> FileResponse:
    allowed = {"model.stl", "analysis.json", "plan.json", "ai-plan.json", "cam.FCStd", "program.nc", "toolpath.json", "verification.json", "simulation.json", "collision.json"}
    generated_artifact = (
        Path(filename).name == filename
        and ((filename.startswith("program-") and filename.endswith(".nc"))
             or (filename.startswith("camotics-") and filename.endswith(".stl")))
    )
    if filename not in allowed and not generated_artifact:
        raise HTTPException(status_code=404, detail="File not found")
    if filename.endswith(".nc"):
        if ai_block_reason := _ai_review_block_reason(job_directory(job_id)):
            raise HTTPException(
                status_code=409,
                detail=f"AI 工艺审查未通过，生产 G-code 已拦截：{ai_block_reason}",
            )
        verification_path = job_directory(job_id) / "verification.json"
        if verification_path.is_file():
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            if verification.get("status") == "failed":
                raise HTTPException(
                    status_code=409,
                    detail="空间成品校验未通过，G-code 已拦截，禁止用于上机加工",
                )
    path = job_directory(job_id) / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    media_types = {
        ".stl": "model/stl",
        ".json": "application/json",
        ".nc": "text/plain",
        ".FCStd": "application/octet-stream",
    }
    media_type = media_types.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=filename)


@app.post("/api/v1/jobs/{job_id}/approve", response_model=JobResponse)
def approve_plan(job_id: str) -> JobResponse:
    job = load_job(job_id)
    if not job.plan:
        raise HTTPException(status_code=409, detail="Plan is not available")
    if job.plan.automation_status == "unsupported":
        raise HTTPException(
            status_code=409,
            detail=(
                f"{job.plan.blocking_reasons[0]} 工艺方案不可批准"
                if job.plan.blocking_reasons
                else "当前零件超出自动 CAM 能力范围，工艺方案不可批准"
            ),
        )
    if ai_block_reason := _ai_review_block_reason(job_directory(job_id)):
        raise HTTPException(
            status_code=409,
            detail=f"AI 工艺审查阻止批准：{ai_block_reason}",
        )
    for setup in job.plan.setups:
        for operation in setup.operations:
            if operation.enabled:
                operation.status = "approved"
    save_job(job_directory(job_id), job)
    write_json(job_directory(job_id) / "plan.json", job.plan.model_dump(mode="json"))
    return job


@app.patch("/api/v1/jobs/{job_id}/features/{feature_id}", response_model=JobResponse)
def review_feature(job_id: str, feature_id: str, request: FeatureReviewRequest) -> JobResponse:
    job = load_job(job_id)
    if not job.analysis:
        raise HTTPException(status_code=409, detail="Geometry analysis is not available")
    feature = next(
        (
            item for item in [
                *job.analysis.cylindrical_features,
                *job.analysis.prismatic_features,
            ] if item.id == feature_id
        ),
        None,
    )
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    feature.review_state = request.review_state
    if request.review_state == "accepted":
        feature.confidence = max(feature.confidence, 0.9)
        reason = "制造工程师已人工确认该特征"
        if reason not in feature.review_reasons:
            feature.review_reasons.append(reason)
    elif request.review_state == "excluded":
        reason = "制造工程师已从自动工艺规划中排除该特征"
        if reason not in feature.review_reasons:
            feature.review_reasons.append(reason)
    job.plan = build_process_plan(
        job.analysis,
        material=job.material,
        machine=job.machine,
        safety=job.plan.safety if job.plan else None,
    )
    directory = job_directory(job_id)
    (directory / "ai-plan.json").unlink(missing_ok=True)
    save_job(directory, job)
    write_json(directory / "analysis.json", job.analysis.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    return job


@app.patch("/api/v1/jobs/{job_id}/safety", response_model=JobResponse)
def update_safety(job_id: str, request: SafetyConfigurationRequest) -> JobResponse:
    job = load_job(job_id)
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis and process plan are required")
    job.plan.safety = build_safety_configuration(
        job.analysis,
        clearance_mm=request.clearance_mm,
        vise_grip_height_mm=request.vise_grip_height_mm,
        support_thickness_mm=request.support_thickness_mm,
        setup_axes=[(setup.id, setup.work_axis) for setup in job.plan.setups],
    )
    for setup in job.plan.setups:
        for operation in setup.operations:
            operation.status = "proposed"
    directory = job_directory(job_id)
    (directory / "ai-plan.json").unlink(missing_ok=True)
    save_job(directory, job)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    return job


def _find_setup(job: JobResponse, setup_id: str):
    if not job.plan:
        raise HTTPException(status_code=409, detail="工艺方案不可用")
    setup = next((item for item in job.plan.setups if item.id == setup_id), None)
    if setup is None:
        raise HTTPException(status_code=404, detail="装夹不存在")
    return setup


def _feature_type(job: JobResponse, feature_id: str) -> str | None:
    if not job.analysis:
        return None
    planar = next((item for item in job.analysis.planar_features if item.id == feature_id), None)
    if planar:
        return "planar_face"
    cylindrical = next((item for item in job.analysis.cylindrical_features if item.id == feature_id), None)
    if cylindrical:
        return "cylindrical_hole" if cylindrical.kind == "hole" else "cylindrical_face"
    prismatic = next((item for item in job.analysis.prismatic_features if item.id == feature_id), None)
    if prismatic:
        return f"prismatic_{prismatic.kind}"
    return None


def _validate_operation_geometry(job: JobResponse, definition, feature_ids: list[str]) -> None:
    requirement = definition.geometry
    if len(feature_ids) < requirement.minimum_selection:
        raise HTTPException(status_code=422, detail=f"{definition.name}至少需要选择 {requirement.minimum_selection} 个几何对象")
    if requirement.maximum_selection is not None and len(feature_ids) > requirement.maximum_selection:
        raise HTTPException(status_code=422, detail=f"{definition.name}最多允许选择 {requirement.maximum_selection} 个几何对象")
    unknown = [feature_id for feature_id in feature_ids if _feature_type(job, feature_id) is None]
    if unknown:
        raise HTTPException(status_code=422, detail=f"找不到几何引用: {', '.join(unknown)}")
    incompatible = [
        feature_id for feature_id in feature_ids
        if _feature_type(job, feature_id) not in requirement.accepts
    ]
    if incompatible:
        actual = ", ".join(f"{item}:{_feature_type(job, item)}" for item in incompatible)
        raise HTTPException(status_code=422, detail=f"所选几何不适用于{definition.name}: {actual}")


def _save_manual_plan_change(job_id: str, job: JobResponse) -> JobResponse:
    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    if job.analysis and job.plan:
        job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    save_job(directory, job)
    if job.plan:
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    return job


@app.post("/api/v1/jobs/{job_id}/setups/{setup_id}/operations", response_model=JobResponse)
def create_manual_operation(job_id: str, setup_id: str, request: OperationCreateRequest) -> JobResponse:
    job = load_job(job_id)
    setup = _find_setup(job, setup_id)
    try:
        definition = get_operation_definition(request.definition_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not definition.manual_enabled:
        raise HTTPException(status_code=409, detail=f"{definition.name}尚未通过当前执行引擎验证")
    _validate_operation_geometry(job, definition, request.feature_ids)
    try:
        tool = get_tool(request.tool_id or definition.tool.default_tool_id)
        if tool.kind not in definition.tool.accepts:
            raise ValueError(f"刀具类型 {tool.kind} 不适用于 {definition.name}")
        existing = [operation for item in job.plan.setups for operation in item.operations] if job.plan else []
        next_sequence = max((operation.sequence for operation in existing), default=0) + 10
        operation = create_operation_instance(
            id=f"OP{next_sequence}", sequence=next_sequence, type=definition.id,
            name=request.name or definition.name, feature_ids=request.feature_ids, tool=tool,
            parameters=request.parameters, rationale=["制造工程师从工序库手动创建"],
            confidence=1.0, status="proposed", source="manual",
        )
        apply_cutting_parameters(operation, resolve_material(job.material), resolve_machine(job.machine))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    setup.operations.append(operation)
    return _save_manual_plan_change(job_id, job)


@app.patch("/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{operation_id}", response_model=JobResponse)
def update_manual_operation(job_id: str, setup_id: str, operation_id: str, request: OperationUpdateRequest) -> JobResponse:
    job = load_job(job_id)
    setup = _find_setup(job, setup_id)
    operation = next((item for item in setup.operations if item.id == operation_id), None)
    if operation is None:
        raise HTTPException(status_code=404, detail="工序不存在")
    try:
        definition = get_operation_definition(operation.definition_id or operation.type)
        if request.feature_ids is not None:
            _validate_operation_geometry(job, definition, request.feature_ids)
            operation.feature_ids = request.feature_ids
        if request.tool_id is not None:
            tool = get_tool(request.tool_id)
            if tool.kind not in definition.tool.accepts:
                raise ValueError(f"刀具类型 {tool.kind} 不适用于 {definition.name}")
            operation.tool = tool
        if request.parameters is not None:
            operation.parameters = validate_parameters(definition, {**operation.parameters, **request.parameters})
        if request.name is not None:
            operation.name = request.name.strip() or definition.name
        if request.enabled is not None:
            operation.enabled = request.enabled
        apply_cutting_parameters(operation, resolve_material(job.material), resolve_machine(job.machine))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    operation.status = "proposed"
    operation.generation_state = "dirty"
    return _save_manual_plan_change(job_id, job)


@app.delete("/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{operation_id}", response_model=JobResponse)
def delete_manual_operation(job_id: str, setup_id: str, operation_id: str) -> JobResponse:
    job = load_job(job_id)
    setup = _find_setup(job, setup_id)
    original_count = len(setup.operations)
    setup.operations = [item for item in setup.operations if item.id != operation_id]
    if len(setup.operations) == original_count:
        raise HTTPException(status_code=404, detail="工序不存在")
    return _save_manual_plan_change(job_id, job)


@app.post("/api/v1/jobs/{job_id}/setups/{setup_id}/operations/reorder", response_model=JobResponse)
def reorder_manual_operations(job_id: str, setup_id: str, request: OperationReorderRequest) -> JobResponse:
    job = load_job(job_id)
    setup = _find_setup(job, setup_id)
    current = {operation.id: operation for operation in setup.operations}
    if len(request.operation_ids) != len(current) or set(request.operation_ids) != set(current):
        raise HTTPException(status_code=422, detail="排序必须包含当前装夹的全部工序且不能重复")
    setup.operations = [current[operation_id] for operation_id in request.operation_ids]
    for index, operation in enumerate(setup.operations, 1):
        operation.sequence = index * 10
        operation.status = "proposed"
        operation.generation_state = "dirty"
    return _save_manual_plan_change(job_id, job)


@app.get("/api/v1/jobs/{job_id}/cam")
def get_cam_artifact(job_id: str) -> dict[str, object]:
    directory = job_directory(job_id)
    paths = {
        "result": directory / "toolpath.json",
        "verification": directory / "verification.json",
        "simulation": directory / "simulation.json",
        "collision": directory / "collision.json",
    }
    if not all(path.is_file() for path in paths.values()):
        raise HTTPException(status_code=404, detail="CAM artifacts are not available for this job")
    return cam_response(
        job_id,
        json.loads(paths["result"].read_text(encoding="utf-8")),
        json.loads(paths["verification"].read_text(encoding="utf-8")),
        json.loads(paths["simulation"].read_text(encoding="utf-8")),
        json.loads(paths["collision"].read_text(encoding="utf-8")),
    )


def _create_cam_artifact(
    job_id: str,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback({
                "stage": stage,
                "message": message,
                "percent": round(max(0.0, min(percent, 100.0)), 1),
                **details,
            })

    report("starting", "正在准备 CAM 生成环境", 1)
    job = load_job(job_id)
    if not job.plan:
        raise HTTPException(status_code=409, detail="Plan is not available")
    if job.plan.automation_status == "unsupported":
        raise HTTPException(
            status_code=409,
            detail="当前零件超出自动 CAM 能力范围，已阻止生成不完整刀路",
        )
    operations = [operation for setup in job.plan.setups for operation in setup.operations if operation.enabled]
    if not operations or any(operation.status != "approved" for operation in operations):
        raise HTTPException(status_code=409, detail="Every process operation must be approved before CAM generation")
    if job.plan.process_kind == "sheet_forming":
        if not job.analysis:
            raise HTTPException(status_code=409, detail="Geometry analysis is not available")
        directory = job_directory(job_id)
        for index, operation in enumerate(operations, 1):
            operation.generation_state = "generating"
            report(
                "forming_stage", f"正在构建 {operation.name}",
                5 + index / len(operations) * 75,
                operation_id=operation.id, current=index, total=len(operations),
            )
            operation.generation_state = "generated"
        result, verification, simulation, collision = build_forming_preview(job.analysis, job.plan)
        write_json(directory / "toolpath.json", result)
        write_json(directory / "verification.json", verification)
        write_json(directory / "simulation.json", simulation)
        write_json(directory / "collision.json", collision)
        save_job(directory, job)
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
        report(
            "completed", "薄板成形工序与分阶段仿真已生成", 100,
            generated=len(operations), verification=verification["status"], collision=collision["status"],
        )
        return cam_response(job_id, result, verification, simulation, collision)
    if job.analysis and job.plan.safety and any(component.setup_id is None for component in job.plan.safety.fixture_components):
        job.plan.safety = build_safety_configuration(
            job.analysis,
            clearance_mm=job.plan.safety.clearance_mm,
            vise_grip_height_mm=job.plan.safety.vise_grip_height_mm,
            support_thickness_mm=job.plan.safety.support_thickness_mm,
            setup_axes=[(setup.id, setup.work_axis) for setup in job.plan.setups],
        )
        save_job(job_directory(job_id), job)
        write_json(job_directory(job_id) / "plan.json", job.plan.model_dump(mode="json"))
    freecad_path = resolve_executable(FREECAD_CMD)
    if not freecad_path:
        raise HTTPException(
            status_code=503,
            detail="FreeCAD CAM runtime is not installed; the adapter boundary is ready",
        )
    directory = job_directory(job_id)
    source_path = directory / job.filename
    analysis_path = directory / "analysis.json"
    plan_path = directory / "plan.json"
    fcstd_path = directory / "cam.FCStd"
    nc_path = directory / "program.nc"
    result_path = directory / "toolpath.json"
    def forward_freecad_progress(payload: dict[str, object]) -> None:
        current = float(payload.get("current", 0) or 0)
        total = max(float(payload.get("total", 1) or 1), 1)
        progress_callback and progress_callback({
            **payload,
            "percent": round(5 + current / total * 65, 1),
        })

    try:
        adapter_arguments = (
            source_path, analysis_path, plan_path, fcstd_path, nc_path, result_path,
        )
        if progress_callback:
            completed = run_freecad_adapter(
                freecad_path,
                CAM_ADAPTER_SCRIPT,
                adapter_arguments,
                progress_callback=forward_freecad_progress,
            )
        else:
            completed = run_freecad_adapter(
                freecad_path,
                CAM_ADAPTER_SCRIPT,
                adapter_arguments,
            )
    except (subprocess.SubprocessError, OSError) as error:
        details = getattr(error, "stderr", None) or str(error)
        raise HTTPException(status_code=502, detail=f"FreeCAD CAM failed: {details[-2000:]}") from error
    if not all(path.is_file() for path in (fcstd_path, nc_path, result_path)):
        raise HTTPException(status_code=502, detail="FreeCAD CAM did not produce every expected artifact")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    camotics_surfaces: list[dict[str, object]] = []
    if resolve_executable(CAMOTICS_CMD):
        setups_by_id = {setup.id: setup for setup in job.plan.setups}
        simulation_setups = result.get("setups", [])
        for simulation_index, setup in enumerate(simulation_setups, 1):
            if not isinstance(setup, dict) or not isinstance(setup.get("program"), str):
                continue
            report(
                "camotics",
                f"正在仿真 {setup.get('setup_id', f'装夹 {simulation_index}')}",
                70 + simulation_index / max(len(simulation_setups), 1) * 12,
                setup_id=setup.get("setup_id"),
                current=simulation_index,
                total=len(simulation_setups),
            )
            program_name = Path(setup["program"]).name
            if program_name != setup["program"]:
                setup["camotics_error"] = "unsafe_program_filename"
                continue
            program_path = directory / program_name
            safe_setup_id = "".join(
                character if character.isalnum() or character in "-_" else "_"
                for character in str(setup.get("setup_id", "setup"))
            )
            surface_name = f"camotics-{safe_setup_id}.stl"
            surface_path = directory / surface_name
            try:
                setup_plan = setups_by_id.get(str(setup.get("setup_id")))
                if setup_plan is None:
                    raise ValueError("setup_not_found_in_process_plan")
                project_path = write_camotics_project(directory, setup_plan, setup, program_name, 0.5)
                simulation_run = run_camotics(CAMOTICS_CMD, project_path, surface_path)
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                details = getattr(error, "stderr", None) or str(error)
                setup["camotics_error"] = details[-1000:]
                surface_path.unlink(missing_ok=True)
                continue
            if surface_path.is_file() and surface_path.stat().st_size > 84:
                setup["camotics_surface"] = surface_name
                camotics_surfaces.append({
                    "setup_id": setup.get("setup_id"),
                    "file": surface_name,
                    "engine": "CAMotics",
                    "resolution_mm": 0.5,
                    "frame": setup.get("frame"),
                    "stdout": simulation_run.stdout[-500:],
                })
            else:
                setup["camotics_error"] = "CAMotics returned an empty stock-removal surface"
                surface_path.unlink(missing_ok=True)
    result["camotics_surfaces"] = camotics_surfaces
    result["simulation_backend"] = (
        "cumulative-height-field-with-camotics-per-setup"
        if camotics_surfaces else "cumulative-height-field"
    )
    write_json(result_path, result)
    generated_operation_ids = set(result.get("generated_operations", []))
    for operation in operations:
        operation.generation_state = "generated" if operation.id in generated_operation_ids else "failed"
    report("preflight", "正在校验工序覆盖、刀具与机床行程", 85)
    verification = verify_cam(job.plan, result)
    if not job.analysis:
        raise HTTPException(status_code=409, detail="Geometry analysis is not available")
    report("simulation", "正在累计双面高精度余料", 89)
    simulation = simulate_material_removal(
        job.analysis,
        job.plan,
        result,
        progress_callback=lambda message, fraction: report(
            "simulation",
            message,
            89 + max(0.0, min(fraction, 1.0)) * 5,
        ),
    )
    simulation_path = directory / "simulation.json"
    write_json(simulation_path, simulation)
    cumulative_surface = simulation.get("surface", {})
    has_cumulative_stock = isinstance(cumulative_surface, dict) and cumulative_surface.get("is_cumulative") is True
    if has_cumulative_stock:
        target_volume = float(job.analysis.measurements.get("volume", 0))
        remaining_volume = float(simulation.get("metrics", {}).get("remaining_volume_mm3", 0))
        initial_stock_volume = float(simulation.get("metrics", {}).get("initial_stock_volume_mm3", 0))
        conformance_status, deviation, stock_deviation = volume_conformance(
            target_volume,
            remaining_volume,
            initial_stock_volume,
        )
        verification["checks"].append({
            "id": "target_volume_conformance",
            "status": conformance_status,
            "message": (
                f"累计装夹仿真剩余体积与 STEP 目标偏差 {deviation:.1f}%，"
                f"占初始毛坯 {stock_deviation:.2f}%（双面高度场网格近似）"
            ),
        })
        verification["metrics"]["target_volume_mm3"] = round(target_volume, 2)
        verification["metrics"]["remaining_volume_mm3"] = round(remaining_volume, 2)
        verification["metrics"]["target_volume_deviation_percent"] = round(deviation, 2)
        verification["metrics"]["stock_volume_deviation_percent"] = round(stock_deviation, 3)
        model_path = directory / "model.stl"
        if model_path.is_file():
            report("conformance", "正在比对余料与 STEP 空间形状", 95)
            spatial = compare_stock_to_target_mesh(cumulative_surface, model_path)
            verification["metrics"].update(spatial)
            verification["checks"].append({
                "id": "target_spatial_conformance",
                "status": spatial["status"],
                "message": (
                    f"余料与 STEP 空间重合 {spatial['target_overlap_percent']:.1f}%；"
                    f"目标缺失 {spatial['missing_target_volume_mm3']:.2f} mm³，"
                    f"多余残料 {spatial['excess_stock_volume_mm3']:.2f} mm³"
                ),
            })
            if spatial["status"] == "failed":
                verification["errors"].append("加工余料与 STEP 目标的空间形状不一致，存在过切或残料")
                verification["status"] = "failed"
                blocking_reason = (
                    f"CAM 后验空间校验失败：STEP 重合率 {spatial['target_overlap_percent']:.1f}%，"
                    f"过切 {spatial['missing_target_volume_mm3']:.2f} mm³，"
                    f"残料 {spatial['excess_stock_volume_mm3']:.2f} mm³。"
                )
                job.plan.automation_status = "unsupported"
                if blocking_reason not in job.plan.blocking_reasons:
                    job.plan.blocking_reasons.append(blocking_reason)
            elif spatial["status"] == "warning" and verification["status"] == "passed":
                verification["warnings"].append("加工余料与 STEP 目标仍存在需要复核的局部差异")
                verification["status"] = "warning"
        if conformance_status == "failed":
            verification["errors"].append("全部装夹累计加工后的材料体积与 STEP 目标差异过大")
            verification["status"] = "failed"
        elif conformance_status == "warning" and verification["status"] == "passed":
            verification["warnings"].append("累计装夹仿真与目标体积存在明显偏差")
            verification["status"] = "warning"
    elif result.get("profile_boundaries"):
        verification["checks"].append({
            "id": "target_volume_conformance",
            "status": "warning",
            "message": "当前装夹方向无法汇入累计余料，未进行最终成品体积判定",
        })
        verification["warnings"].append("最终成品体积需要体素或实体布尔仿真复核")
        if verification["status"] == "passed":
            verification["status"] = "warning"
    verification_path = directory / "verification.json"
    write_json(verification_path, verification)
    report("collision", "正在执行刀具、刀柄与夹具碰撞检查", 96)
    collision = detect_collisions(job.analysis, job.plan, result)
    collision_path = directory / "collision.json"
    write_json(collision_path, collision)
    full_preview_count = len(result.get("preview_segments", []))
    result["preview_segments"] = compact_preview_segments(list(result.get("preview_segments", [])))
    result["preview_segment_count_raw"] = full_preview_count
    result["preview_segment_count"] = len(result["preview_segments"])
    write_json(result_path, result)
    verified_minutes = verification.get("metrics", {}).get("estimated_cycle_minutes")
    if isinstance(verified_minutes, (int, float)):
        job.plan.estimated_minutes = float(verified_minutes)
    save_job(directory, job)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    response = cam_response(
        job_id, result, verification, simulation, collision,
        stdout=completed.stdout[-1000:],
    )
    report(
        "completed", "CAM 刀路与仿真已完成", 100,
        generated=len(generated_operation_ids),
        verification=verification.get("status"),
        collision=collision.get("status"),
    )
    return response


@app.post("/api/v1/jobs/{job_id}/cam")
def create_cam_artifact(job_id: str) -> dict[str, object]:
    return _create_cam_artifact(job_id)


@app.get("/api/v1/jobs/{job_id}/cam/stream")
def stream_cam_artifact(job_id: str) -> StreamingResponse:
    # Validate before starting the streaming response so ordinary HTTP errors
    # still reach clients with their proper status code.
    job = load_job(job_id)
    if not job.plan:
        raise HTTPException(status_code=409, detail="Plan is not available")
    with CAM_STREAM_LOCK:
        if job_id in CAM_STREAMING_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在生成刀路")
        CAM_STREAMING_JOBS.add(job_id)

    events: Queue[dict[str, object]] = Queue()

    def worker() -> None:
        try:
            _create_cam_artifact(job_id, progress_callback=events.put)
        except HTTPException as error:
            events.put({"stage": "error", "message": str(error.detail), "percent": 100})
        except Exception as error:
            events.put({"stage": "error", "message": str(error), "percent": 100})
        finally:
            with CAM_STREAM_LOCK:
                CAM_STREAMING_JOBS.discard(job_id)

    threading.Thread(target=worker, name=f"cam-stream-{job_id[:8]}", daemon=True).start()

    def event_stream():
        terminal = False
        while not terminal:
            try:
                event = events.get(timeout=15)
            except Empty:
                yield ": keepalive\n\n"
                continue
            terminal = event.get("stage") in {"completed", "error"}
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
