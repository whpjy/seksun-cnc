from __future__ import annotations

import json
from math import radians, tan
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
    AxialDrillingOperationReviewRequest, BoringOperationReviewRequest, FeatureReviewRequest, GeometryAnalysis, JobHistoryItem, JobResponse, OperationCreateRequest,
    ManufacturingRequirementsImportRequest, OperationReorderRequest, OperationUpdateRequest,
    ProcessPlan, SafetyConfigurationRequest, ThreadBindingConfirmRequest,
    ThreadOperationReviewRequest,
    SolidSelectionRequest,
)
from .planner import build_process_plan
from .catalogs import apply_cutting_parameters, catalog_payload, get_tool, resolve_machine, resolve_material
from .operation_library import (
    create_operation_instance, get_operation_definition, operation_library_payload, validate_parameters,
)
from .device_library import device_library_payload, get_device
from .l32_configuration import l32_definition_payload, snapshot_l32_instance
from .machine_models import MachineBindingRequest, MachineConfigurationSnapshot, MachineInstance
from .manufacturing_knowledge import assess_plan_knowledge, get_manufacturing_process, manufacturing_process_payload
from .route_planner import build_manufacturing_route
from .requirements_adapter import import_measurement_specification, reconcile_requirement_bindings
from .measurement_client import MeasurementServiceError, analyze_pdf_step
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
from .remediation import apply_automatic_remediation, build_remediation_report
from .rotational_features import (
    RotationalFeatureAnalysis, bind_thread_requirements, infer_rotational_features,
)
from .turning_draft import TurningDraftRequest, TurningDraftResult, compile_turning_draft
from .turning_reachability import assess_turning_reachability
from cam.providers.turning import TurningContext
from .turning_transfer import (
    TurningTransferDraftRequest, TurningTransferDraftResult,
    compile_synchronized_transfer_draft,
)
from .l32_backside import BacksideDraftRequest, BacksideDraftResult, compile_backside_draft
from .l32_whole_program import WholePartDraftRequest, WholePartDraftResult, compile_whole_part_draft


APP_ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = Path(os.getenv("CNC_STORAGE_ROOT", APP_ROOT / ".seksun-cnc" / "jobs"))
ANALYZER_BIN = os.getenv("CNC_ANALYZER_BIN", "occt-analyzer")
FREECAD_CMD = os.getenv("CNC_FREECAD_CMD", "FreeCADCmd")
CAMOTICS_CMD = os.getenv("CNC_CAMOTICS_CMD", "camsim")
CAM_ADAPTER_SCRIPT = Path(os.getenv("CNC_CAM_ADAPTER_SCRIPT", APP_ROOT / "cam" / "freecad_adapter.py"))
MAX_UPLOAD_BYTES = int(os.getenv("CNC_MAX_UPLOAD_MB", "200")) * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("CNC_PUBLIC_BASE_URL", "").rstrip("/")
MEAS_API_BASE_URL = os.getenv("CNC_MEAS_API_BASE_URL", "").strip().rstrip("/")
MEAS_TIMEOUT_SECONDS = max(float(os.getenv("CNC_MEAS_TIMEOUT_SECONDS", "600")), 1.0)
BENCHMARK_REPORT_PATH = Path(os.getenv(
    "CNC_BENCHMARK_REPORT", STORAGE_ROOT.parent / "benchmark-report.json",
))
CAM_STREAM_LOCK = threading.Lock()
CAM_STREAMING_JOBS: set[str] = set()
AI_REVIEW_LOCK = threading.Lock()
AI_REVIEWING_JOBS: set[str] = set()
JOB_EVENT_CONDITION = threading.Condition()
JOB_EVENT_LOGS: dict[str, list[dict[str, object]]] = {}


def publish_job_event(job_id: str, stage: str, message: str, percent: float, **details: object) -> None:
    event = {
        "stage": stage,
        "message": message,
        "percent": max(0, min(round(percent, 1), 100)),
        "created_at": utc_now(),
        **{key: value for key, value in details.items() if value is not None},
    }
    with JOB_EVENT_CONDITION:
        JOB_EVENT_LOGS.setdefault(job_id, []).append(event)
        JOB_EVENT_CONDITION.notify_all()

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


def persist_rotational_analysis(
    directory: Path, job: JobResponse, analysis: GeometryAnalysis,
) -> RotationalFeatureAnalysis | None:
    path = directory / "rotational-features.json"
    is_l32 = job.device_id == "citizen-cincom-l32" or "citizen cincom l32" in job.machine.lower()
    if not is_l32:
        path.unlink(missing_ok=True)
        return None
    result = infer_rotational_features(analysis)
    requirements = job.plan.manufacturing_requirements if job.plan else None
    result = bind_thread_requirements(result, requirements)
    write_json(path, result.model_dump(mode="json"))
    return result


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
        "simulation.json", "collision.json", "remediation.json", "remediation-history.json", "ai-plan.json",
        "turning-toolpath-ir.json", "turning-simulation.json", "turning-verification.json", "turning-thread-verification.json", "turning-reachability.json", "turning-draft.json",
        "turning-transfer-ir.json", "turning-transfer-draft.json",
        "turning-backside-ir.json", "turning-backside-draft.json",
        "turning-whole-program-ir.json", "turning-whole-program-draft.json",
        "turning-whole-program-timeline.json", "turning-continuous-simulation.json",
        "boring-reachability.json", "axial-drilling-review.json",
    ):
        (directory / filename).unlink(missing_ok=True)
    for pattern in ("program-*.nc", "camotics-*.stl", "*.camotics"):
        for artifact in directory.glob(pattern):
            if artifact.is_file() and artifact.parent == directory:
                artifact.unlink(missing_ok=True)


def run_geometry_analyzer(
    source_path: Path,
    analysis_path: Path,
    model_path: Path,
    solid_index: int | None = None,
) -> GeometryAnalysis:
    command = [ANALYZER_BIN, str(source_path), str(analysis_path), str(model_path)]
    if solid_index is not None:
        command.append(str(solid_index))
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    analysis = GeometryAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
    analysis = normalize_manufacturing_features(analysis)
    write_json(analysis_path, analysis.model_dump(mode="json"))
    return analysis


def cam_response(
    job_id: str,
    result: dict[str, object],
    verification: dict[str, object],
    simulation: dict[str, object],
    collision: dict[str, object],
    remediation: dict[str, object] | None = None,
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
            "remediation": f"/api/v1/jobs/{job_id}/files/remediation.json",
            "camotics": [
                f"/api/v1/jobs/{job_id}/files/{surface['file']}"
                for surface in result.get("camotics_surfaces", [])
                if isinstance(surface, dict) and isinstance(surface.get("file"), str)
            ],
        },
        "verification": verification,
        "simulation": simulation,
        "collision": collision,
        "remediation": remediation,
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


@app.get("/api/v1/device-library")
def get_device_library() -> dict[str, object]:
    return device_library_payload()


@app.get("/api/v1/device-library/{device_id}")
def get_device_library_item(device_id: str) -> dict[str, object]:
    try:
        return get_device(device_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def machine_instance_path(instance_id: str) -> Path:
    if (
        not instance_id
        or len(instance_id) > 64
        or not instance_id[0].isalnum()
        or any(not (character.isalnum() or character in "_.-") for character in instance_id)
    ):
        raise HTTPException(status_code=404, detail="Machine instance not found")
    return STORAGE_ROOT / ".machine-instances" / f"{instance_id}.json"


def load_machine_snapshot(path: Path) -> MachineConfigurationSnapshot:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Machine instance not found")
    try:
        return MachineConfigurationSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Stored machine instance is invalid") from error


@app.get("/api/v1/machines/l32/definitions")
def get_l32_machine_definition() -> dict[str, object]:
    return l32_definition_payload()


@app.post("/api/v1/machines/l32/instances", response_model=MachineConfigurationSnapshot)
def create_l32_machine_instance(instance: MachineInstance) -> MachineConfigurationSnapshot:
    snapshot = snapshot_l32_instance(instance)
    path = machine_instance_path(instance.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, snapshot.model_dump(mode="json"))
    return snapshot


@app.get("/api/v1/machines/l32/instances/{instance_id}", response_model=MachineConfigurationSnapshot)
def get_l32_machine_instance(instance_id: str) -> MachineConfigurationSnapshot:
    return load_machine_snapshot(machine_instance_path(instance_id))


@app.put("/api/v1/jobs/{job_id}/machine-instance", response_model=JobResponse)
def bind_job_machine_instance(job_id: str, request: MachineBindingRequest) -> JobResponse:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Machine instance binding currently requires an L32 job")
    snapshot = load_machine_snapshot(machine_instance_path(request.machine_instance_id))
    if snapshot.instance.definition_id != "citizen-cincom-l32":
        raise HTTPException(status_code=422, detail="Machine instance is not a Citizen Cincom L32")
    if not snapshot.validation.valid:
        raise HTTPException(status_code=422, detail="Machine instance configuration contains blocking issues")
    if job.plan:
        stock_diameter = float(job.plan.stock.get("diameter_mm", 0) or 0)
        if stock_diameter > snapshot.instance.bar_diameter_mm + 1e-9:
            raise HTTPException(status_code=422, detail="Planned stock diameter exceeds the machine instance configuration")
        required_option = job.plan.stock.get("required_option")
        if required_option and required_option not in snapshot.instance.enabled_options:
            raise HTTPException(status_code=422, detail=f"Machine instance lacks required option: {required_option}")
        job.plan.stock["machine_instance_id"] = snapshot.instance.id
        job.plan.stock["machine_configuration_hash"] = snapshot.configuration_hash
        back_turning_enabled = "back_turning" in snapshot.validation.capabilities
        for setup in job.plan.setups:
            for operation in setup.operations:
                if operation.workpiece_side == "back":
                    operation.enabled = back_turning_enabled
                    operation.generation_state = "dirty"
        warning = "当前绑定设备实例未确认 back_turning 刀具模块，OP50/OP60 背面工序保持禁用。"
        if back_turning_enabled:
            job.plan.warnings = [item for item in job.plan.warnings if item != warning]
        else:
            if warning not in job.plan.warnings:
                job.plan.warnings.append(warning)
        job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan) if job.analysis else job.plan.coverage
        job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan) if job.analysis else job.plan.manufacturing_route
        job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan) if job.analysis else job.plan.knowledge_assessment

    directory = job_directory(job_id)
    job.machine_instance_id = snapshot.instance.id
    job.machine_configuration_hash = snapshot.configuration_hash
    invalidate_cam_artifacts(directory)
    write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))
    if job.plan:
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


@app.get(
    "/api/v1/jobs/{job_id}/machine-instance",
    response_model=MachineConfigurationSnapshot,
)
def get_job_machine_instance(job_id: str) -> MachineConfigurationSnapshot:
    job = load_job(job_id)
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=404, detail="Job does not have a bound machine instance")
    snapshot = load_machine_snapshot(job_directory(job_id) / "machine-configuration.json")
    if (
        snapshot.instance.id != job.machine_instance_id
        or snapshot.configuration_hash != job.machine_configuration_hash
    ):
        raise HTTPException(status_code=409, detail="Bound machine configuration integrity check failed")
    return snapshot


@app.post("/api/v1/jobs/{job_id}/turning/draft", response_model=TurningDraftResult)
def generate_turning_draft(job_id: str, request: TurningDraftRequest) -> TurningDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Turning draft generation requires an L32 job")
    if job.status != "completed" or job.analysis is None:
        raise HTTPException(status_code=409, detail="Geometry analysis must complete before draft generation")
    if request.operation.type == "turn_threading":
        planned = next(
            (
                item for setup in (job.plan.setups if job.plan else []) for item in setup.operations
                if item.id == request.operation.id and item.type == "turn_threading"
            ),
            None,
        )
        if planned is None or planned.model_dump(mode="json") != request.operation.model_dump(mode="json"):
            raise HTTPException(status_code=409, detail="Thread draft must exactly match the stored reviewed operation")
        if (
            not planned.enabled
            or planned.parameters.get("engineering_review_status") != "verified_engineer"
        ):
            raise HTTPException(status_code=409, detail="Thread operation has not completed DRAFT-level engineering review")
    elif (
        request.operation.type in {"turn_id_roughing", "turn_id_finishing"}
        or (request.operation.type == "axial_drilling" and request.operation.source == "automatic")
    ):
        planned = next(
            (
                item for setup in (job.plan.setups if job.plan else []) for item in setup.operations
                if item.id == request.operation.id and item.type == request.operation.type
            ),
            None,
        )
        if planned is None or planned.model_dump(mode="json") != request.operation.model_dump(mode="json"):
            raise HTTPException(status_code=409, detail="Inner-feature draft must exactly match the stored reviewed operation")
        if (
            not planned.enabled
            or planned.parameters.get("engineering_review_status") != "verified_engineer"
        ):
            raise HTTPException(status_code=409, detail="Inner-feature operation has not completed DRAFT-level engineering review")

    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before draft generation")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Draft request does not use the machine instance bound to this job")
    snapshot_path = job_directory(job_id) / "machine-configuration.json"
    try:
        snapshot = load_machine_snapshot(snapshot_path)
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        if request.profile is not None:
            rotational_path = job_directory(job_id) / "rotational-features.json"
            if not rotational_path.is_file():
                raise ValueError("stored rotational analysis is not available")
            rotational = RotationalFeatureAnalysis.model_validate_json(
                rotational_path.read_text(encoding="utf-8")
            )
            stored_profile = next(
                (item for item in rotational.profiles if item.id == request.profile.id), None,
            )
            if stored_profile is None or stored_profile.review_state != "accepted":
                raise ValueError("stored rotational profile must be accepted before draft generation")
            if stored_profile.model_dump(mode="json") != request.profile.model_dump(mode="json"):
                raise ValueError("submitted rotational profile does not match the accepted stored profile")
        result = compile_turning_draft(job_id, request, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    directory = job_directory(job_id)
    write_json(directory / "turning-toolpath-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-simulation.json", result.simulation.model_dump(mode="json"))
    if result.verification is not None:
        write_json(directory / "turning-verification.json", result.verification.model_dump(mode="json"))
    if result.thread_verification is not None:
        write_json(
            directory / "turning-thread-verification.json",
            result.thread_verification.model_dump(mode="json"),
        )
    if result.reachability is not None:
        write_json(directory / "turning-reachability.json", result.reachability.model_dump(mode="json"))
    write_json(directory / "turning-draft.json", result.model_dump(mode="json"))
    return result


@app.post(
    "/api/v1/jobs/{job_id}/turning/transfer/draft",
    response_model=TurningTransferDraftResult,
)
def generate_turning_transfer_draft(
    job_id: str, request: TurningTransferDraftRequest,
) -> TurningTransferDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Synchronized transfer draft requires an L32 job")
    if job.status != "completed" or job.analysis is None:
        raise HTTPException(status_code=409, detail="Geometry analysis must complete before transfer planning")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before transfer planning")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Transfer request does not use the machine instance bound to this job")
    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        if request.profile is None:
            raise ValueError("synchronized transfer requires an accepted rotational profile")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        stored_profile = next(
            (item for item in rotational.profiles if item.id == request.profile.id), None,
        )
        if stored_profile is None or stored_profile.review_state != "accepted":
            raise ValueError("stored rotational profile must be accepted before transfer planning")
        if stored_profile.model_dump(mode="json") != request.profile.model_dump(mode="json"):
            raise ValueError("submitted rotational profile does not match the accepted stored profile")
        result = compile_synchronized_transfer_draft(job_id, request, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-transfer-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-transfer-draft.json", result.model_dump(mode="json"))
    return result


@app.post(
    "/api/v1/jobs/{job_id}/turning/backside/draft",
    response_model=BacksideDraftResult,
)
def generate_turning_backside_draft(
    job_id: str, request: BacksideDraftRequest,
) -> BacksideDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Backside turning draft requires an L32 process plan")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before backside planning")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Backside request does not use the machine instance bound to this job")
    planned_operation = next((
        operation for setup in job.plan.setups for operation in setup.operations
        if operation.id == request.operation.id and operation.workpiece_side == "back"
    ), None)
    if planned_operation is None or not planned_operation.enabled:
        raise HTTPException(status_code=409, detail="Backside operation is not enabled by the bound machine configuration")
    if (
        request.operation.type != planned_operation.type
        or request.operation.tool.id != planned_operation.tool.id
        or request.operation.feature_ids != planned_operation.feature_ids
    ):
        raise HTTPException(status_code=422, detail="Submitted backside operation does not match the formal process plan")

    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        source_profile = next((
            item for item in rotational.profiles if item.id == request.source_profile_id
        ), None)
        if source_profile is None or source_profile.review_state != "accepted":
            raise ValueError("source rotational profile must be accepted before backside planning")
        result = compile_backside_draft(job_id, request, source_profile, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-backside-ir.json", result.draft.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-backside-draft.json", result.model_dump(mode="json"))
    return result


@app.post(
    "/api/v1/jobs/{job_id}/turning/whole-program/draft",
    response_model=WholePartDraftResult,
)
def generate_turning_whole_program_draft(
    job_id: str, request: WholePartDraftRequest,
) -> WholePartDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Whole-part turning draft requires an L32 process plan")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before whole-part planning")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Whole-part request does not use the machine instance bound to this job")
    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        source_profile = next((
            item for item in rotational.profiles if item.id == request.source_profile_id
        ), None)
        if source_profile is None or source_profile.review_state != "accepted":
            raise ValueError("source rotational profile must be accepted before whole-part planning")
        result = compile_whole_part_draft(
            job_id, request, job.plan, source_profile, snapshot,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-whole-program-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-timeline.json", result.timeline.model_dump(mode="json"))
    write_json(directory / "turning-continuous-simulation.json", result.continuous_simulation.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-draft.json", result.model_dump(mode="json"))
    return result


@app.get("/api/v1/manufacturing-processes")
def get_manufacturing_processes(
    family: str | None = None, query: str | None = None, compact: bool = False,
) -> dict[str, object]:
    return manufacturing_process_payload(family=family, query=query, compact=compact)


@app.get("/api/v1/manufacturing-processes/{code}")
def get_manufacturing_process_item(code: str) -> dict[str, object]:
    try:
        return get_manufacturing_process(code)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/v1/config")
def get_config() -> dict[str, object]:
    return {
        "public_base_url": PUBLIC_BASE_URL,
        "session_sharing": True,
        "cam_engine": "freecad-cam",
        "simulation_engine": "camotics-with-heightfield-fallback",
        "drawing_intelligence": {
            "configured": bool(MEAS_API_BASE_URL),
            "provider": "seksun-meas",
        },
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
            ("remediation", "remediation.json"),
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


def _process_new_job(
    job_id: str,
    *,
    progress_callback: Callable[..., None] | None = None,
    ai_assisted: bool = False,
) -> JobResponse:
    job = load_job(job_id)
    directory = job_directory(job_id)
    source_path = directory / job.filename
    drawing_path = directory / "drawing.pdf"
    analysis_path = directory / "analysis.json"
    model_path = directory / "model.stl"

    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, percent, **details)

    try:
        report("geometry_analysis", "正在解析 STEP 拓扑与制造特征", 12)
        analysis = run_geometry_analyzer(source_path, analysis_path, model_path)
        rotational_analysis = persist_rotational_analysis(directory, job, analysis)
        feature_count = (
            len(analysis.planar_features) + len(analysis.cylindrical_features)
            + len(analysis.prismatic_features) + len(analysis.internal_profile_features)
        )
        report(
            "geometry_analysis", "三维几何分析完成", 34,
            feature_count=feature_count,
            hole_count=sum(item.kind == "hole" and item.review_state != "excluded" for item in analysis.cylindrical_features),
            rotational_status=rotational_analysis.status if rotational_analysis else None,
        )

        report("draft_planning", "正在生成确定性工艺草案", 40)
        plan = build_process_plan(analysis, material=job.material, machine=job.machine)
        requirements = None
        report("drawing_analysis", "正在提取图纸尺寸、公差和技术要求", 46)
        try:
            specification, measurement_link = analyze_pdf_step(
                MEAS_API_BASE_URL, drawing_path, source_path,
                timeout_seconds=MEAS_TIMEOUT_SECONDS,
            )
            requirements = reconcile_requirement_bindings(
                import_measurement_specification(specification), analysis,
            )
            plan = build_process_plan(
                analysis, material=job.material, machine=job.machine,
                requirements=requirements,
            )
            job.measurement_job_id = measurement_link.get("measurement_job_id")
            write_json(directory / "manufacturing-specification.json", specification)
            write_json(directory / "manufacturing-requirements.json", requirements.model_dump(mode="json"))
            write_json(directory / "measurement-link.json", measurement_link)
            report(
                "drawing_analysis", "二维图纸要求提取完成", 58,
                requirement_count=len(requirements.requirements),
                matched_count=requirements.summary.get("matched", 0),
            )
        except MeasurementServiceError as error:
            plan.warnings.append(f"二维图纸识别未完成：{error}")
            report("drawing_analysis", "图纸解析未完成，已使用三维几何继续规划", 58, warning=str(error))

        if ai_assisted:
            report("ai_planning", "AI 正在判断制造意图、装夹路线与工序策略", 64)
            try:
                guidance = review_process_plan(analysis, plan)
                write_json(directory / "planning-guidance.json", guidance)
                review = guidance.get("review", {})
                recommended_kind = str(review.get("recommended_process_kind", ""))
                confidence = float(review.get("confidence", 0) or 0)
                process_kind_hint = (
                    "sheet_forming"
                    if recommended_kind == "sheet_forming" and confidence >= 0.7
                    else None
                )
                if process_kind_hint and plan.process_kind != process_kind_hint:
                    report("process_generation", "正在按 AI 制造意图重新编译工艺路线", 73)
                    plan = build_process_plan(
                        analysis, material=job.material, machine=job.machine,
                        requirements=requirements, process_kind_hint=process_kind_hint,
                    )
                plan.ai_planning = {
                    "provider": guidance.get("provider"),
                    "model": guidance.get("model"),
                    "created_at": guidance.get("created_at"),
                    "manufacturing_intent": review.get("manufacturing_intent"),
                    "recommended_process_kind": recommended_kind,
                    "part_family": review.get("part_family"),
                    "confidence": confidence,
                    "summary": review.get("summary"),
                    "requires_engineer_review": review.get("requires_engineer_review", False),
                }
                report(
                    "ai_planning", "AI 规划意图已纳入工艺编译", 72,
                    manufacturing_intent=review.get("manufacturing_intent"),
                    recommended_process_kind=recommended_kind,
                    confidence=confidence,
                )
            except QwenPlanningError as error:
                plan.ai_planning = {"status": "fallback", "message": str(error)}
                plan.warnings.append("AI 辅助规划不可用，本次已回退到确定性规则规划")
                report("ai_planning", "AI 暂不可用，已自动回退到规则规划", 72, warning=str(error))

        report("process_generation", "正在生成装夹、工序、刀具与切削参数", 78)
        plan.coverage = evaluate_plan_coverage(analysis, plan)
        plan.manufacturing_route = build_manufacturing_route(analysis, plan)
        plan.knowledge_assessment = assess_plan_knowledge(analysis, plan)
        operation_count = sum(len(setup.operations) for setup in plan.setups)
        report(
            "coverage_validation", "正在检查工艺覆盖率和 CAM 能力", 88,
            setup_count=len(plan.setups), operation_count=operation_count,
            coverage_score=plan.coverage.score if plan.coverage else None,
        )

        write_json(directory / "plan.json", plan.model_dump(mode="json"))
        job.status = "completed"
        job.analysis = analysis
        job.plan = plan
        persist_rotational_analysis(directory, job, analysis)
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
        save_job(directory, job)
        report(
            "completed", "工艺方案已生成", 100,
            setup_count=len(plan.setups), operation_count=operation_count,
            coverage_score=plan.coverage.score if plan.coverage else None,
        )
        return job
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        job.status = "failed"
        details = getattr(error, "stderr", None) or str(error)
        job.error = details[-2000:]
        save_job(directory, job)
        report("error", job.error or "工艺生成失败", 100)
        return job


@app.post("/api/v1/jobs", response_model=JobResponse)
async def create_job(
    step: UploadFile = File(...),
    drawing: UploadFile = File(...),
    material: str = Form("待确认（候选：6061-T6 铝合金）"),
    machine: str = Form("待确认（候选：VMC850 三轴立式加工中心）"),
    device_id: str | None = Form(None),
) -> JobResponse:
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")
    drawing_filename = Path(drawing.filename or "drawing.pdf").name
    if Path(drawing_filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF drawings are supported")
    if device_id:
        try:
            machine = str(get_device(device_id)["name"])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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

    drawing_path = directory / "drawing.pdf"
    drawing_size = 0
    with drawing_path.open("wb") as output:
        while chunk := await drawing.read(1024 * 1024):
            drawing_size += len(chunk)
            if drawing_size > MAX_UPLOAD_BYTES:
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(status_code=413, detail="PDF drawing exceeds upload limit")
            output.write(chunk)

    job = JobResponse(
        id=job_id,
        status="processing",
        filename=filename,
        created_at=utc_now(),
        material=material,
        machine=machine,
        device_id=device_id,
        drawing_filename=drawing_filename,
        drawing_url=f"/api/v1/jobs/{job_id}/files/drawing.pdf",
    )
    save_job(directory, job)

    return _process_new_job(job_id)


@app.post("/api/v1/jobs/start", response_model=JobResponse)
async def start_job(
    step: UploadFile = File(...),
    drawing: UploadFile = File(...),
    material: str = Form("待确认（候选：6061-T6 铝合金）"),
    machine: str = Form("待确认（候选：VMC850 三轴立式加工中心）"),
    device_id: str | None = Form(None),
) -> JobResponse:
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")
    drawing_filename = Path(drawing.filename or "drawing.pdf").name
    if Path(drawing_filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Only PDF drawings are supported")
    if device_id:
        try:
            machine = str(get_device(device_id)["name"])
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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

    drawing_path = directory / "drawing.pdf"
    drawing_size = 0
    with drawing_path.open("wb") as output:
        while chunk := await drawing.read(1024 * 1024):
            drawing_size += len(chunk)
            if drawing_size > MAX_UPLOAD_BYTES:
                shutil.rmtree(directory, ignore_errors=True)
                raise HTTPException(status_code=413, detail="PDF drawing exceeds upload limit")
            output.write(chunk)

    job = JobResponse(
        id=job_id,
        status="processing",
        filename=filename,
        created_at=utc_now(),
        material=material,
        machine=machine,
        device_id=device_id,
        drawing_filename=drawing_filename,
        drawing_url=f"/api/v1/jobs/{job_id}/files/drawing.pdf",
    )
    save_job(directory, job)
    with JOB_EVENT_CONDITION:
        JOB_EVENT_LOGS[job_id] = []
    publish_job_progress = lambda stage, message, percent, **details: publish_job_event(
        job_id, stage, message, percent, **details,
    )
    publish_job_progress("uploading", "二维图纸和三维模型上传完成", 6)

    def worker() -> None:
        try:
            _process_new_job(
                job_id, progress_callback=publish_job_progress, ai_assisted=True,
            )
        except Exception as error:
            failed = load_job(job_id)
            failed.status = "failed"
            failed.error = str(error)[-2000:]
            save_job(directory, failed)
            publish_job_progress("error", failed.error or "工艺生成失败", 100)

    threading.Thread(target=worker, name=f"job-plan-{job_id[:8]}", daemon=True).start()
    return job


@app.get("/api/v1/jobs/{job_id}/events")
def stream_job_progress(job_id: str) -> StreamingResponse:
    load_job(job_id)

    def event_stream():
        index = 0
        terminal = False
        while not terminal:
            event = None
            with JOB_EVENT_CONDITION:
                events = JOB_EVENT_LOGS.get(job_id, [])
                if index >= len(events):
                    JOB_EVENT_CONDITION.wait(timeout=15)
                    events = JOB_EVENT_LOGS.get(job_id, [])
                if index < len(events):
                    event = events[index]
                    index += 1
            if event is None:
                current = load_job(job_id)
                if current.status == "completed":
                    event = {"stage": "completed", "message": "工艺方案已生成", "percent": 100}
                elif current.status == "failed":
                    event = {"stage": "error", "message": current.error or "工艺生成失败", "percent": 100}
                else:
                    yield ": keepalive\n\n"
                    continue
            terminal = event.get("stage") in {"completed", "error"}
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return load_job(job_id)


@app.get("/api/v1/jobs", response_model=list[JobHistoryItem])
def list_jobs() -> list[JobHistoryItem]:
    if not STORAGE_ROOT.is_dir():
        return []
    history: list[JobHistoryItem] = []
    for directory in STORAGE_ROOT.iterdir():
        if not directory.is_dir() or not (directory / "job.json").is_file():
            continue
        try:
            job = load_job(directory.name)
        except (HTTPException, OSError, ValueError, json.JSONDecodeError):
            continue
        operations = [operation for setup in job.plan.setups for operation in setup.operations] if job.plan else []
        history.append(JobHistoryItem(
            id=job.id,
            status=job.status,
            filename=job.filename,
            created_at=job.created_at,
            material=job.material,
            machine=job.machine,
            process_kind=job.plan.process_kind if job.plan else None,
            setup_count=len(job.plan.setups) if job.plan else 0,
            operation_count=len(operations),
        ))
    return sorted(history, key=lambda item: item.created_at, reverse=True)


@app.delete("/api/v1/jobs")
def clear_job_history(preserve_job_id: str | None = None) -> dict[str, int]:
    """Delete stored jobs while keeping the task currently open in the UI."""
    if preserve_job_id is not None:
        job_directory(preserve_job_id)
    if not STORAGE_ROOT.is_dir():
        return {"deleted_count": 0}

    deleted_job_ids: list[str] = []
    for directory in STORAGE_ROOT.iterdir():
        if (
            not directory.is_dir()
            or directory.name == preserve_job_id
            or not (directory / "job.json").is_file()
        ):
            continue
        shutil.rmtree(directory)
        deleted_job_ids.append(directory.name)

    if deleted_job_ids:
        with JOB_EVENT_CONDITION:
            for job_id in deleted_job_ids:
                JOB_EVENT_LOGS.pop(job_id, None)
    return {"deleted_count": len(deleted_job_ids)}


@app.get("/api/v1/jobs/{job_id}/manufacturing-route")
def get_job_manufacturing_route(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis or process plan is not available")
    route = build_manufacturing_route(job.analysis, job.plan)
    plan = job.plan.model_copy(deep=True)
    plan.manufacturing_route = route
    assessment = assess_plan_knowledge(job.analysis, plan)
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "route": route.model_dump(mode="json"),
        "knowledge_assessment": assessment.model_dump(mode="json"),
    }


@app.post("/api/v1/jobs/{job_id}/turning/analyze", response_model=RotationalFeatureAnalysis)
def analyze_job_rotational_features(job_id: str) -> RotationalFeatureAnalysis:
    job = load_job(job_id)
    if not job.analysis:
        raise HTTPException(status_code=409, detail="Geometry analysis is not available")
    result = persist_rotational_analysis(job_directory(job_id), job, job.analysis)
    if result is None:
        raise HTTPException(status_code=409, detail="Rotational analysis is only enabled for an L32 job")
    return result


@app.patch(
    "/api/v1/jobs/{job_id}/turning/profiles/{profile_id}",
    response_model=RotationalFeatureAnalysis,
)
def review_job_rotational_profile(
    job_id: str, profile_id: str, request: FeatureReviewRequest,
) -> RotationalFeatureAnalysis:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Rotational profile review requires an L32 job")
    path = job_directory(job_id) / "rotational-features.json"
    if not path.is_file():
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    try:
        analysis = RotationalFeatureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Stored rotational analysis is invalid") from error
    profile = next((item for item in analysis.profiles if item.id == profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="Rotational profile not found")
    directory = job_directory(job_id)
    if not job.analysis:
        raise HTTPException(status_code=409, detail="Geometry analysis is required")
    job.analysis.rotational_profile_reviews[profile_id] = request.review_state
    profile.review_state = request.review_state
    if not job.plan:
        write_json(directory / "analysis.json", job.analysis.model_dump(mode="json"))
        write_json(path, analysis.model_dump(mode="json"))
        save_job(directory, job)
        invalidate_cam_artifacts(directory)
        return analysis
    previous_plan = job.plan
    previous_operations = {
        item.id: item.model_copy(deep=True)
        for setup in previous_plan.setups for item in setup.operations
    }
    job.plan = build_process_plan(
        job.analysis,
        material=job.material,
        machine=job.machine,
        safety=previous_plan.safety,
        requirements=previous_plan.manufacturing_requirements,
    )
    for key in ("machine_instance_id", "machine_configuration_hash"):
        if key in previous_plan.stock:
            job.plan.stock[key] = previous_plan.stock[key]
    for setup in job.plan.setups:
        for index, operation in enumerate(setup.operations):
            previous = previous_operations.get(operation.id)
            if previous is None or previous.type != operation.type:
                continue
            if previous.parameters.get("engineering_review_status") == "verified_engineer":
                setup.operations[index] = previous
            elif previous.workpiece_side == "back" and previous.enabled:
                operation.enabled = True
    write_json(directory / "analysis.json", job.analysis.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    result = persist_rotational_analysis(directory, job, job.analysis)
    assert result is not None
    save_job(directory, job)
    invalidate_cam_artifacts(directory)
    return result


@app.post("/api/v1/jobs/{job_id}/manufacturing-requirements", response_model=JobResponse)
def import_job_manufacturing_requirements(
    job_id: str, request: ManufacturingRequirementsImportRequest,
) -> JobResponse:
    job = load_job(job_id)
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis or process plan is not available")
    try:
        requirements = reconcile_requirement_bindings(
            import_measurement_specification(
                request.specification, source_system=request.source_system,
            ),
            job.analysis,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    job.plan.manufacturing_requirements = requirements
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
    for setup in job.plan.setups:
        for operation in setup.operations:
            if operation.enabled:
                operation.status = "proposed"
                operation.generation_state = "dirty"
    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "manufacturing-requirements.json", requirements.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    persist_rotational_analysis(directory, job, job.analysis)
    save_job(directory, job)
    return job


@app.post(
    "/api/v1/jobs/{job_id}/turning/thread-bindings/confirm",
    response_model=JobResponse,
)
def confirm_job_thread_binding(
    job_id: str, request: ThreadBindingConfirmRequest,
) -> JobResponse:
    """Confirm one drawing thread against one periodic STEP tooth-form candidate.

    This creates only a disabled planning operation. It does not authorize
    production NC or infer missing run-out, tooling, or controller decisions.
    """
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Thread binding confirmation requires an L32 job")
    if not job.analysis or not job.plan or not job.plan.manufacturing_requirements:
        raise HTTPException(status_code=409, detail="Geometry analysis and drawing requirements are required")

    requirements = job.plan.manufacturing_requirements.model_copy(deep=True)
    requirement = next(
        (item for item in requirements.requirements if item.id == request.requirement_id), None,
    )
    if requirement is None:
        raise HTTPException(status_code=404, detail="Drawing thread requirement not found")
    if requirement.type != "thread" or requirement.thread is None:
        raise HTTPException(status_code=422, detail="Selected drawing requirement is not a parsed thread")
    if requirement.thread.side == "internal":
        raise HTTPException(status_code=422, detail="Current periodic tooth-form detector only supports external threads")

    rotational = infer_rotational_features(job.analysis)
    feature = next(
        (
            item for item in rotational.features
            if item.id == request.thread_feature_id and item.kind == "thread_form_candidate"
        ),
        None,
    )
    if feature is None:
        raise HTTPException(status_code=404, detail="STEP periodic thread-form candidate not found")
    pitch = requirement.thread.pitch_mm
    if not any(
        abs(candidate - pitch) <= max(pitch * 0.02, 0.02)
        for candidate in feature.pitch_candidates_mm
    ):
        raise HTTPException(status_code=422, detail="Drawing pitch does not match the STEP periodic tooth form")
    modelled_major_diameter = (
        max(feature.radius_start, feature.radius_end) + feature.depth_mm
    ) * 2
    if abs(modelled_major_diameter - requirement.thread.major_diameter_mm) > max(pitch * 0.75, 0.3):
        raise HTTPException(status_code=422, detail="Drawing major diameter does not match the STEP tooth form")

    requirement.mapping_status = "matched"
    requirement.verification_status = "verified_engineer"
    requirement.cad_feature_ids = [feature.id]
    requirement.source["binding_method"] = "cnc_periodic_thread_engineer_confirmation"
    requirement.source["confirmed_thread_feature_id"] = feature.id
    requirement.source["confirmed_at"] = utc_now()
    requirements.unresolved_requirement_ids = [
        item.id for item in requirements.requirements
        if item.mapping_status in {"ambiguous", "unmapped", "not_applicable"}
        or not item.verification_status.startswith("verified")
    ]
    requirements.summary = {
        "total": len(requirements.requirements),
        "matched": sum(item.mapping_status == "matched" for item in requirements.requirements),
        "ambiguous": sum(item.mapping_status == "ambiguous" for item in requirements.requirements),
        "unmapped": sum(item.mapping_status == "unmapped" for item in requirements.requirements),
        "recognized_only": sum(item.mapping_status == "not_applicable" for item in requirements.requirements),
    }
    requirements.status = (
        "complete" if requirements.requirements and not requirements.unresolved_requirement_ids
        else "review" if requirements.summary["matched"] else "incomplete"
    )

    previous_plan = job.plan
    previous_operations = {
        item.id: item.model_copy(deep=True)
        for setup in previous_plan.setups for item in setup.operations
    }
    job.plan = build_process_plan(
        job.analysis,
        material=job.material,
        machine=job.machine,
        safety=previous_plan.safety,
        requirements=requirements,
    )
    for key in ("machine_instance_id", "machine_configuration_hash"):
        if key in previous_plan.stock:
            job.plan.stock[key] = previous_plan.stock[key]
    previously_enabled = {
        operation.id
        for setup in previous_plan.setups
        for operation in setup.operations
        if operation.enabled and operation.workpiece_side == "back"
    }
    for setup in job.plan.setups:
        for index, operation in enumerate(setup.operations):
            previous = previous_operations.get(operation.id)
            if (
                previous is not None
                and previous.type == operation.type
                and previous.parameters.get("engineering_review_status") == "verified_engineer"
            ):
                setup.operations[index] = previous
                continue
            if operation.id in previously_enabled and operation.workpiece_side == "back":
                operation.enabled = True

    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "manufacturing-requirements.json", requirements.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    persist_rotational_analysis(directory, job, job.analysis)
    save_job(directory, job)
    return job


@app.post(
    "/api/v1/jobs/{job_id}/turning/thread-operations/{operation_id}/review",
    response_model=JobResponse,
)
def review_job_thread_operation(
    job_id: str, operation_id: str, request: ThreadOperationReviewRequest,
) -> JobResponse:
    """Approve a bound thread operation for controller-independent DRAFT simulation."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Thread operation review requires an L32 job")
    if not job.analysis or not job.plan or not job.plan.manufacturing_requirements:
        raise HTTPException(status_code=409, detail="Geometry, plan, and drawing requirements are required")
    operation = next(
        (
            item for setup in job.plan.setups for item in setup.operations
            if item.id == operation_id
        ),
        None,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Thread operation not found")
    if operation.type != "turn_threading" or operation.source != "automatic":
        raise HTTPException(status_code=422, detail="Only an automatically bound thread draft can use this review")

    rotational = persist_rotational_analysis(job_directory(job_id), job, job.analysis)
    if rotational is None:
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    feature = next(
        (
            item for item in rotational.features
            if item.kind == "thread_form_candidate" and item.id in operation.feature_ids
        ),
        None,
    )
    if feature is None or feature.binding_state != "matched" or feature.resolved_pitch_mm is None:
        raise HTTPException(status_code=409, detail="Thread operation does not have a verified drawing-to-STEP binding")
    verified_requirements = [
        item for item in job.plan.manufacturing_requirements.requirements
        if item.id in feature.drawing_requirement_ids
        and item.mapping_status == "matched"
        and item.verification_status == "verified_engineer"
    ]
    if len(verified_requirements) != 1:
        raise HTTPException(status_code=409, detail="Thread operation requires exactly one engineer-verified drawing requirement")

    pitch = feature.resolved_pitch_mm
    minimum_z = min(feature.z_start, feature.z_end)
    maximum_z = max(feature.z_start, feature.z_end)
    if request.start_z_mm <= request.end_z_mm:
        raise HTTPException(status_code=422, detail="L32 main-spindle thread start Z must be greater than end Z")
    if (
        request.start_z_mm > maximum_z + pitch
        or request.start_z_mm < minimum_z - pitch
        or request.end_z_mm > maximum_z + pitch
        or request.end_z_mm < minimum_z - pitch
    ):
        raise HTTPException(status_code=422, detail="Reviewed thread limits exceed the detected tooth-form region")
    if request.relief_strategy == "groove" and request.relief_width_mm < pitch * 0.5:
        raise HTTPException(status_code=422, detail="Relief groove width must be at least half of the thread pitch")
    if request.thread_depth_mm > pitch:
        raise HTTPException(status_code=422, detail="Reviewed thread depth is implausibly larger than the pitch")
    major_diameter = float(operation.parameters["major_diameter_mm"])
    minor_diameter = round(major_diameter - 2 * request.thread_depth_mm, 6)
    if minor_diameter <= 0:
        raise HTTPException(status_code=422, detail="Reviewed thread depth produces an invalid minor diameter")

    operation.parameters.update({
        "start_z_mm": request.start_z_mm,
        "end_z_mm": request.end_z_mm,
        "thread_depth_mm": request.thread_depth_mm,
        "minor_diameter_mm": minor_diameter,
        "pass_count": request.pass_count,
        "relief_strategy": request.relief_strategy,
        "relief_width_mm": request.relief_width_mm,
        "tool_insert_id": request.tool_insert_id,
        "controller_cycle_id": request.controller_cycle_id,
        "engineering_review_status": "verified_engineer",
        "engineering_reviewer": request.reviewer,
        "engineering_reviewed_at": utc_now(),
    })
    if feature.profile_id not in operation.feature_ids:
        operation.feature_ids.insert(0, feature.profile_id)
    operation.enabled = True
    operation.status = "warning"
    operation.generation_state = "dirty"
    review_reason = "螺纹起止、牙深、退刀策略、刀片和控制器循环已完成 DRAFT 级工程审核"
    if review_reason not in operation.rationale:
        operation.rationale.append(review_reason)
    job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)

    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


@app.post(
    "/api/v1/jobs/{job_id}/turning/axial-drilling-operations/{operation_id}/review",
    response_model=JobResponse,
)
def review_job_axial_drilling_operation(
    job_id: str, operation_id: str, request: AxialDrillingOperationReviewRequest,
) -> JobResponse:
    """Approve a pre-bore drill after its physical tool and tip clearance are confirmed."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Axial drilling review requires an L32 job")
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis and process plan are required")
    operation = next(
        (
            item for setup in job.plan.setups for item in setup.operations
            if item.id == operation_id
        ),
        None,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Axial drilling operation not found")
    if operation.type != "axial_drilling" or operation.source != "automatic":
        raise HTTPException(status_code=422, detail="Only an automatically planned pre-bore operation can use this review")

    rotational = persist_rotational_analysis(job_directory(job_id), job, job.analysis)
    profile = next(
        (
            item for item in (rotational.profiles if rotational else [])
            if item.id in operation.feature_ids and item.side == "inner"
        ),
        None,
    )
    if profile is None or profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise HTTPException(status_code=409, detail="Pre-bore review requires an accepted exact inner profile")
    try:
        drill = get_tool(request.drill_tool_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if drill.kind != "drill":
        raise HTTPException(status_code=422, detail="Selected tool is not a drill")
    required_diameter = float(operation.parameters.get("required_initial_bore_diameter_mm", 0))
    maximum_diameter = float(operation.parameters.get("maximum_prebore_diameter_mm", 0))
    if drill.diameter_mm + 1e-9 < required_diameter:
        raise HTTPException(status_code=422, detail="Drill diameter is smaller than the rough-boring entry requirement")
    if maximum_diameter <= 0 or drill.diameter_mm > maximum_diameter + 1e-9:
        raise HTTPException(status_code=422, detail="Drill diameter consumes the required finish-boring allowance")
    if request.peck_depth_mm > drill.diameter_mm + 1e-9:
        raise HTTPException(status_code=422, detail="Peck depth cannot exceed the confirmed drill diameter")

    profile_depth = float(operation.parameters.get("profile_depth_mm", 0))
    tip_length = drill.diameter_mm / 2 / tan(radians(request.drill_point_angle_deg / 2))
    if (
        request.bottom_condition == "blind_tip_allowance_confirmed"
        and request.tip_overtravel_allowance_mm + 1e-9 < tip_length
    ):
        raise HTTPException(status_code=422, detail="Confirmed blind-hole tip allowance is smaller than the drill tip length")
    programmed_depth = profile_depth + tip_length
    if request.confirmed_stickout_mm + 1e-9 < programmed_depth + 2.0:
        raise HTTPException(status_code=422, detail="Confirmed drill stickout does not cover depth plus axial clearance")
    if drill.flute_length_mm + 1e-9 < programmed_depth:
        raise HTTPException(status_code=422, detail="Catalog drill flute length does not cover the programmed depth")

    drill.stickout_mm = request.confirmed_stickout_mm
    operation.tool = drill
    operation.parameters.update({
        "depth_mm": round(programmed_depth, 6),
        "full_diameter_depth_mm": profile_depth,
        "drill_point_angle_deg": request.drill_point_angle_deg,
        "drill_tip_length_mm": round(tip_length, 6),
        "peck_depth_mm": request.peck_depth_mm,
        "bottom_condition": request.bottom_condition,
        "tip_overtravel_allowance_mm": request.tip_overtravel_allowance_mm,
        "confirmed_stickout_mm": request.confirmed_stickout_mm,
        "drill_inventory_id": request.drill_inventory_id,
        "engineering_review_status": "verified_engineer",
        "engineering_reviewer": request.reviewer,
        "engineering_reviewed_at": utc_now(),
    })
    operation.enabled = True
    operation.status = "warning"
    operation.generation_state = "dirty"
    reason = "钻头实物、有效刃长、伸出、啄钻深度和钻尖越程已通过 DRAFT 级审核"
    if reason not in operation.rationale:
        operation.rationale.append(reason)
    job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "axial-drilling-review.json", {
        "schema_version": "1.0.0", "operation_id": operation.id,
        "profile_id": profile.id, "drill_tool_id": drill.id,
        "drill_diameter_mm": drill.diameter_mm, "profile_depth_mm": profile_depth,
        "programmed_depth_mm": programmed_depth, "tip_length_mm": tip_length,
        "reviewer": request.reviewer,
    })
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


@app.post(
    "/api/v1/jobs/{job_id}/turning/boring-operations/{operation_id}/review",
    response_model=JobResponse,
)
def review_job_boring_operation(
    job_id: str, operation_id: str, request: BoringOperationReviewRequest,
) -> JobResponse:
    """Approve one exact inner-profile operation for DRAFT simulation."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Boring operation review requires an L32 job")
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis and process plan are required")
    operation = next(
        (
            item for setup in job.plan.setups for item in setup.operations
            if item.id == operation_id
        ),
        None,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Boring operation not found")
    if operation.type not in {"turn_id_roughing", "turn_id_finishing"} or operation.source != "automatic":
        raise HTTPException(status_code=422, detail="Only an automatically planned inner-profile operation can use this review")

    rotational = persist_rotational_analysis(job_directory(job_id), job, job.analysis)
    if rotational is None:
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    profile = next(
        (
            item for item in rotational.profiles
            if item.id in operation.feature_ids and item.side == "inner"
        ),
        None,
    )
    if profile is None or profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise HTTPException(status_code=409, detail="Boring review requires an accepted exact inner profile")

    prebore = next(
        (
            item for setup in job.plan.setups for item in setup.operations
            if item.type == "axial_drilling"
            and item.source == "automatic"
            and profile.id in item.feature_ids
        ),
        None,
    )
    if prebore is not None:
        if (
            not prebore.enabled
            or prebore.parameters.get("engineering_review_status") != "verified_engineer"
        ):
            raise HTTPException(status_code=409, detail="Review and enable the planned pre-bore before approving boring")
        if abs(prebore.tool.diameter_mm - request.initial_bore_diameter_mm) > 1e-6:
            raise HTTPException(status_code=422, detail="Initial bore diameter must match the reviewed pre-bore drill")
        required_depth = float(operation.parameters.get("profile_depth_mm", 0))
        available_depth = float(prebore.parameters.get("full_diameter_depth_mm", 0))
        if available_depth + 1e-9 < required_depth:
            raise HTTPException(status_code=422, detail="Reviewed pre-bore does not cover the inner-profile depth")

    reviewed_operation = operation.model_copy(deep=True)
    reviewed_operation.tool.stickout_mm = request.confirmed_stickout_mm
    stock_radius = float(job.plan.stock.get("diameter_mm", 0) or 0) / 2
    if stock_radius <= 0 or request.initial_bore_diameter_mm / 2 >= stock_radius:
        raise HTTPException(status_code=422, detail="Initial bore must be smaller than the selected bar radius")
    context = TurningContext(
        machine_snapshot_hash=job.machine_configuration_hash or "0" * 64,
        stock_radius_mm=stock_radius,
        initial_bore_radius_mm=request.initial_bore_diameter_mm / 2,
        radial_clearance_mm=max(request.assembly_clearance_mm, 0.01),
    )
    reachability = assess_turning_reachability(
        reviewed_operation, profile, context,
        assembly_clearance_mm=request.assembly_clearance_mm,
    )
    if reachability.status == "failed":
        raise HTTPException(
            status_code=422,
            detail="Boring reachability failed: " + "; ".join(reachability.blocking_reasons),
        )

    operation.tool = reviewed_operation.tool
    operation.parameters.update({
        "initial_bore_diameter_mm": request.initial_bore_diameter_mm,
        "confirmed_stickout_mm": request.confirmed_stickout_mm,
        "assembly_clearance_mm": request.assembly_clearance_mm,
        "boring_bar_inventory_id": request.boring_bar_inventory_id,
        "engineering_review_status": "verified_engineer",
        "engineering_reviewer": request.reviewer,
        "engineering_reviewed_at": utc_now(),
    })
    operation.enabled = True
    operation.status = "warning"
    operation.generation_state = "dirty"
    review_reason = "初始孔、镗杆入口、径向空间、隐藏倒扣和轴向伸出已通过 DRAFT 级审核"
    if review_reason not in operation.rationale:
        operation.rationale.append(review_reason)
    job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "boring-reachability.json", reachability.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


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
        previous_rotational_reviews = dict(job.analysis.rotational_profile_reviews) if job.analysis else {}
        selected_index = int(job.analysis.topology.get("selected_solid_index", 0)) if job.analysis else 0
        analysis = run_geometry_analyzer(
            source_path, analysis_path, model_path, selected_index or None,
        )
        analysis.rotational_profile_reviews = previous_rotational_reviews
        write_json(analysis_path, analysis.model_dump(mode="json"))
        persist_rotational_analysis(directory, job, analysis)
        plan = build_process_plan(
            analysis, material=job.material, machine=job.machine,
            requirements=job.plan.manufacturing_requirements if job.plan else None,
        )
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


@app.patch("/api/v1/jobs/{job_id}/solid", response_model=JobResponse)
def select_job_solid(job_id: str, request: SolidSelectionRequest) -> JobResponse:
    job = load_job(job_id)
    if not job.analysis:
        raise HTTPException(status_code=409, detail="Geometry analysis is not available")
    available_indices = {candidate.index for candidate in job.analysis.solid_candidates}
    source_solid_count = int(job.analysis.topology.get("source_solids", 1))
    if available_indices and request.solid_index not in available_indices:
        raise HTTPException(status_code=400, detail="Solid index is not available")
    if not available_indices and not 1 <= request.solid_index <= source_solid_count:
        raise HTTPException(status_code=400, detail="Solid index is not available")

    directory = job_directory(job_id)
    source_path = directory / job.filename
    if not source_path.is_file():
        raise HTTPException(status_code=409, detail="Original STEP file is not available")
    current_index = int(job.analysis.topology.get("selected_solid_index", 0))
    if current_index == request.solid_index:
        return job

    invalidate_cam_artifacts(directory)
    analysis_path = directory / "analysis.json"
    model_path = directory / "model.stl"
    try:
        analysis = run_geometry_analyzer(
            source_path, analysis_path, model_path, request.solid_index,
        )
        persist_rotational_analysis(directory, job, analysis)
        plan = build_process_plan(
            analysis, material=job.material, machine=job.machine,
            requirements=job.plan.manufacturing_requirements if job.plan else None,
        )
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        details = getattr(error, "stderr", None) or str(error)
        raise HTTPException(status_code=422, detail=details[-2000:]) from error

    job.status = "completed"
    job.error = None
    job.analysis = analysis
    job.plan = plan
    write_json(directory / "plan.json", plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


@app.get("/api/v1/jobs/{job_id}/files/{filename}")
def get_job_file(job_id: str, filename: str) -> FileResponse:
    allowed = {
        "model.stl", "drawing.pdf", "analysis.json", "rotational-features.json", "plan.json",
        "manufacturing-specification.json", "manufacturing-requirements.json", "measurement-link.json",
        "planning-guidance.json",
        "ai-plan.json", "cam.FCStd", "program.nc", "toolpath.json", "verification.json",
        "simulation.json", "collision.json", "remediation.json", "remediation-history.json",
        "turning-toolpath-ir.json", "turning-simulation.json", "turning-verification.json", "turning-reachability.json", "turning-draft.json",
        "turning-transfer-ir.json", "turning-transfer-draft.json",
        "turning-backside-ir.json", "turning-backside-draft.json",
        "turning-whole-program-ir.json", "turning-whole-program-draft.json",
        "turning-whole-program-timeline.json", "turning-continuous-simulation.json",
    }
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
                *job.analysis.internal_profile_features,
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
        requirements=job.plan.manufacturing_requirements if job.plan else None,
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
        job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
        job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
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
    if (
        operation.type in {"turn_threading", "turn_id_roughing", "turn_id_finishing", "axial_drilling"}
        and operation.source == "automatic"
        and (request.parameters is not None or request.tool_id is not None or request.enabled is not None)
    ):
        raise HTTPException(status_code=409, detail="自动规划的螺纹/内孔工序必须通过专用工程审核接口修改或启用")
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
    result = json.loads(paths["result"].read_text(encoding="utf-8"))
    verification = json.loads(paths["verification"].read_text(encoding="utf-8"))
    simulation = json.loads(paths["simulation"].read_text(encoding="utf-8"))
    collision = json.loads(paths["collision"].read_text(encoding="utf-8"))
    remediation_path = directory / "remediation.json"
    remediation = json.loads(remediation_path.read_text(encoding="utf-8")) if remediation_path.is_file() else None
    if remediation is None:
        job = load_job(job_id)
        if job.analysis and job.plan:
            history_path = directory / "remediation-history.json"
            history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
            remediation = build_remediation_report(
                job.analysis, job.plan, result, verification, collision,
                iteration=len(history) if isinstance(history, list) else 0,
            )
            write_json(remediation_path, remediation)
    return cam_response(
        job_id, result, verification, simulation, collision, remediation=remediation,
    )


def _apply_cam_remediation(job_id: str, *, auto_approve: bool = False) -> dict[str, object]:
    """Apply deterministic low-risk corrections and invalidate stale CAM artifacts."""
    job = load_job(job_id)
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="几何分析或工艺方案不可用")
    directory = job_directory(job_id)
    remediation_path = directory / "remediation.json"
    if not remediation_path.is_file():
        raise HTTPException(status_code=409, detail="请先生成刀路和仿真，再执行缺陷修复")
    remediation = json.loads(remediation_path.read_text(encoding="utf-8"))
    if not remediation.get("can_auto_replan"):
        raise HTTPException(status_code=409, detail="当前缺陷包含必须由工程师复核的高风险项，不能自动修改")

    history_path = directory / "remediation-history.json"
    history = (
        json.loads(history_path.read_text(encoding="utf-8"))
        if history_path.is_file()
        else []
    )
    if not isinstance(history, list):
        history = []
    applied_actions = apply_automatic_remediation(job.plan, remediation)
    if not applied_actions:
        raise HTTPException(status_code=409, detail="没有可安全自动执行的修复动作")

    if any(item["kind"] == "adjust_safety" for item in applied_actions) and job.plan.safety:
        job.plan.safety = build_safety_configuration(
            job.analysis,
            clearance_mm=job.plan.safety.clearance_mm,
            vise_grip_height_mm=job.plan.safety.vise_grip_height_mm,
            support_thickness_mm=job.plan.safety.support_thickness_mm,
            setup_axes=[(setup.id, setup.work_axis) for setup in job.plan.setups],
        )
    if auto_approve:
        for setup in job.plan.setups:
            for operation in setup.operations:
                if operation.enabled:
                    operation.status = "approved"
    job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
    history.append({
        "iteration": len(history) + 1,
        "applied_at": utc_now(),
        "source_report": {
            "status": remediation.get("status"),
            "defect_ids": [item.get("id") for item in remediation.get("defects", []) if isinstance(item, dict)],
        },
        "actions": applied_actions,
    })
    invalidate_cam_artifacts(directory)
    write_json(history_path, history)
    save_job(directory, job)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    return {
        "status": "applied",
        "iteration": len(history),
        "requires_approval": not auto_approve,
        "applied_actions": applied_actions,
        "job": job.model_dump(mode="json"),
    }


@app.post("/api/v1/jobs/{job_id}/cam/remediation/apply")
def apply_cam_remediation(job_id: str) -> dict[str, object]:
    return _apply_cam_remediation(job_id)


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
    if job.device_id == "citizen-cincom-l32":
        raise HTTPException(
            status_code=409,
            detail="L32 当前仅允许通过车削适配工作台生成 DRAFT Toolpath IR；未经认证不得进入通用 NC 生成流程",
        )
    if job.plan.automation_status == "unsupported":
        raise HTTPException(
            status_code=409,
            detail="当前零件超出自动 CAM 能力范围，已阻止生成不完整刀路",
        )
    operations = [operation for setup in job.plan.setups for operation in setup.operations if operation.enabled]
    if not operations:
        raise HTTPException(status_code=409, detail="No enabled process operations are available for CAM generation")
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
        history_path = directory / "remediation-history.json"
        remediation_history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
        remediation = build_remediation_report(
            job.analysis, job.plan, result, verification, collision,
            iteration=len(remediation_history) if isinstance(remediation_history, list) else 0,
        )
        write_json(directory / "toolpath.json", result)
        write_json(directory / "verification.json", verification)
        write_json(directory / "simulation.json", simulation)
        write_json(directory / "collision.json", collision)
        write_json(directory / "remediation.json", remediation)
        save_job(directory, job)
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
        report(
            "completed", "薄板成形工序与分阶段仿真已生成", 100,
            generated=len(operations), verification=verification["status"], collision=collision["status"],
        )
        return cam_response(job_id, result, verification, simulation, collision, remediation)
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
            spatial = compare_stock_to_target_mesh(
                cumulative_surface,
                model_path,
                toolpath_segments=[
                    item for item in result.get("preview_segments", []) if isinstance(item, dict)
                ],
            )
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
    report("remediation", "正在定位缺陷并生成补救建议", 98)
    history_path = directory / "remediation-history.json"
    remediation_history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else []
    remediation = build_remediation_report(
        job.analysis, job.plan, result, verification, collision,
        iteration=len(remediation_history) if isinstance(remediation_history, list) else 0,
    )
    write_json(directory / "remediation.json", remediation)
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
        job_id, result, verification, simulation, collision, remediation,
        stdout=completed.stdout[-1000:],
    )
    report(
        "completed", "CAM 刀路与仿真已完成", 100,
        generated=len(generated_operation_ids),
        verification=verification.get("status"),
        collision=collision.get("status"),
    )
    return response


def _run_cam_remediation_loop(
    job_id: str,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Apply safe corrections and regenerate CAM until validation converges or blocks."""

    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback({
                "stage": stage,
                "message": message,
                "percent": round(max(0.0, min(percent, 100.0)), 1),
                **details,
            })

    latest_result: dict[str, object] | None = None
    while True:
        directory = job_directory(job_id)
        remediation_path = directory / "remediation.json"
        if not remediation_path.is_file():
            raise HTTPException(status_code=409, detail="请先生成刀路和仿真，再启动自动纠错")
        current_report = json.loads(remediation_path.read_text(encoding="utf-8"))
        if not current_report.get("can_auto_replan"):
            raise HTTPException(status_code=409, detail="当前缺陷不能进入自动纠错闭环")

        applied = _apply_cam_remediation(job_id, auto_approve=True)
        iteration = int(applied["iteration"])
        max_iterations = max(int(current_report.get("max_iterations", 3)), 1)
        action_labels = [
            str(item.get("label", ""))
            for item in applied.get("applied_actions", [])
            if isinstance(item, dict)
        ]
        report(
            "remediation_applied",
            f"第 {iteration} 轮：已应用 {len(action_labels)} 项安全修复",
            min((iteration - 1) / max_iterations * 100 + 2, 92),
            iteration=iteration,
            max_iterations=max_iterations,
            actions=action_labels,
        )

        def forward_cam_progress(event: dict[str, object]) -> None:
            inner_percent = float(event.get("percent", 0) or 0)
            overall = ((iteration - 1) + inner_percent / 100) / max_iterations * 94
            stage = str(event.get("stage", "cam"))
            report(
                "revalidation" if stage == "completed" else stage,
                f"第 {iteration} 轮复验：{event.get('message', '正在重新生成刀路')}",
                min(overall, 94),
                iteration=iteration,
                max_iterations=max_iterations,
                operation_id=event.get("operation_id"),
                setup_id=event.get("setup_id"),
                current=event.get("current"),
                total=event.get("total"),
            )

        latest_result = _create_cam_artifact(job_id, progress_callback=forward_cam_progress)
        next_report = latest_result.get("remediation")
        if not isinstance(next_report, dict):
            raise HTTPException(status_code=500, detail="复验未生成缺陷报告")
        defects = next_report.get("defects", [])
        if not defects:
            outcome = "passed"
            message = f"自动纠错在第 {iteration} 轮通过全部复验"
        elif next_report.get("can_auto_replan"):
            report(
                "iteration_retry",
                f"第 {iteration} 轮仍有可修复问题，准备继续迭代",
                min(iteration / max_iterations * 94, 94),
                iteration=iteration,
                max_iterations=max_iterations,
                defect_count=len(defects) if isinstance(defects, list) else 0,
            )
            continue
        elif next_report.get("status") == "blocked":
            outcome = "blocked"
            message = f"第 {iteration} 轮发现高风险问题，已停止自动纠错"
        elif int(next_report.get("iteration", iteration)) >= int(next_report.get("max_iterations", max_iterations)):
            outcome = "max_iterations"
            message = f"已完成 {iteration} 轮自动纠错，问题尚未收敛"
        else:
            outcome = "manual_review"
            message = f"第 {iteration} 轮仍有需要工程师处理的问题"
        report(
            "completed", message, 100,
            mode="remediation_loop",
            outcome=outcome,
            iteration=iteration,
            max_iterations=max_iterations,
            defect_count=len(defects) if isinstance(defects, list) else 0,
        )
        return {**latest_result, "remediation_loop": {"outcome": outcome, "iteration": iteration}}


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


@app.get("/api/v1/jobs/{job_id}/cam/remediation/stream")
def stream_cam_remediation(job_id: str) -> StreamingResponse:
    job = load_job(job_id)
    if not job.plan:
        raise HTTPException(status_code=409, detail="工艺方案不可用")
    remediation_path = job_directory(job_id) / "remediation.json"
    if not remediation_path.is_file():
        raise HTTPException(status_code=409, detail="请先生成刀路和仿真，再启动自动纠错")
    remediation = json.loads(remediation_path.read_text(encoding="utf-8"))
    if not remediation.get("can_auto_replan"):
        raise HTTPException(status_code=409, detail="当前缺陷包含人工复核项，不能启动自动纠错")
    with CAM_STREAM_LOCK:
        if job_id in CAM_STREAMING_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在生成刀路或执行自动纠错")
        CAM_STREAMING_JOBS.add(job_id)

    events: Queue[dict[str, object]] = Queue()

    def worker() -> None:
        try:
            _run_cam_remediation_loop(job_id, progress_callback=events.put)
        except HTTPException as error:
            events.put({"stage": "error", "message": str(error.detail), "percent": 100})
        except Exception as error:
            events.put({"stage": "error", "message": str(error), "percent": 100})
        finally:
            with CAM_STREAM_LOCK:
                CAM_STREAMING_JOBS.discard(job_id)

    threading.Thread(target=worker, name=f"remediation-stream-{job_id[:8]}", daemon=True).start()

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
