from __future__ import annotations

import json
import hashlib
from math import isfinite, radians, tan
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Callable

from fastapi import FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

from .models import (
    AxialDrillingOperationReviewRequest, BoringOperationReviewRequest, FeatureReviewRequest, GeometryAnalysis, GroovingOperationReviewRequest, JobHistoryItem, JobResponse, OperationCreateRequest,
    GrooveBindingConfirmRequest, ManufacturingRequirement, ManufacturingRequirements,
    ManufacturingRequirementsImportRequest, OperationReorderRequest, OperationUpdateRequest,
    L32AutonomousProcessRequest, L32CandidateToolBindingRequest, L32OperationCandidateEvaluationRequest, L32OperationCandidateSelectionRequest,
    L32AIProfileDecisionRequest, L32OperationTrialDecisionRequest, L32ProfileDecisionRequest, Operation, ProcessPlan, SafetyConfigurationRequest, ThreadBindingConfirmRequest,
    ThreadOperationReviewRequest,
    SolidSelectionRequest,
)
from .planner import build_process_plan
from .catalogs import apply_cutting_parameters, catalog_payload, get_tool, resolve_machine, resolve_material
from .operation_library import (
    create_operation_instance, get_operation_definition, operation_library_payload, validate_parameters,
)
from .device_library import device_library_payload, get_device
from .l32_configuration import l32_definition_payload, l32_viii_live_tool_catalog_reference, snapshot_l32_instance
from .l32_indexed_pocket import IndexedPocketDraft, IndexedPocketSweepCheck, build_indexed_back_pocket_draft
from .l32_side_milling import build_l32_exterior_clear_draft, build_l32_side_mill_draft
from .l32_front_groove import build_front_groove_geometry_draft
from .tool_inventory import (
    ToolInventoryInput, ToolInventoryRecord, bind_verified_inventory_tool,
    inventory_binding_evidence_request, physical_tool_fit_for_groove,
    record_physical_tool,
)
from .machine_models import MachineBindingRequest, MachineConfigurationSnapshot, MachineInstance
from .manufacturing_knowledge import assess_plan_knowledge, get_manufacturing_process, manufacturing_process_payload
from .route_planner import build_manufacturing_route
from .requirements_adapter import import_measurement_specification, reconcile_requirement_bindings
from .collision import build_safety_configuration, detect_collisions
from .conformance import compare_stock_to_target_mesh
from .preflight import verify_cam
from .simulation import simulate_material_removal
from .recognizer import normalize_manufacturing_features, recognize_planar_machining_features
from .engines import probe_engine, resolve_executable, run_camotics, run_freecad_adapter
from .forming import build_forming_preview
from .qwen import QwenPlanningError, probe_qwen, qwen_config_payload, review_process_plan
from .agent.process_strategies import (
    ValidationOutcome, evaluate_validation_contract, get_process_strategy,
    operations_for_role,
)
from .benchmarks import example_catalog_payload
from .coverage import evaluate_plan_coverage
from .remediation import apply_automatic_remediation, build_remediation_report
from .rotational_features import (
    AIProvisionalProfileDecision, RotationalFeatureAnalysis, bind_thread_requirements,
    clip_rotational_profile, infer_rotational_features,
)
from .groove_binding import (
    DrawingGrooveRequirement, bind_groove_requirement, groove_candidate_evidence,
)
from .turning_draft import TurningDraftRequest, TurningDraftResult, compile_turning_draft
from .turning_reachability import assess_turning_reachability
from cam.providers.turning import TurningContext
from .turning_transfer import (
    TurningTransferDraftRequest, TurningTransferDraftResult,
    compile_synchronized_transfer_draft,
)
from .l32_backside import BacksideDraftRequest, BacksideDraftResult, compile_backside_draft
from .l32_backside_chain import (
    BacksideChainDraftRequest, BacksideChainDraftResult, compile_backside_chain_draft,
)
from .l32_front_chain import FrontChainDraftRequest, FrontChainDraftResult, compile_front_chain_draft
from .l32_whole_program import WholePartDraftRequest, WholePartDraftResult, compile_whole_part_draft
from .l32_back_live_face import BackLiveFaceDraftRequest, compile_back_live_face_draft
from .inner_bore_chain import InnerBoreChainRequest, InnerBoreChainResult, compile_inner_bore_chain
from .agent.architecture import agent_architecture
from .agent.events import build_agent_event
from .agent.workspace import build_agent_workspace


APP_ROOT = Path(__file__).resolve().parents[1]
STORAGE_ROOT = Path(os.getenv("CNC_STORAGE_ROOT", APP_ROOT / ".seksun-cnc" / "jobs"))
ANALYZER_BIN = os.getenv("CNC_ANALYZER_BIN", "occt-analyzer")
FREECAD_CMD = os.getenv("CNC_FREECAD_CMD", "FreeCADCmd")
CAMOTICS_CMD = os.getenv("CNC_CAMOTICS_CMD", "camsim")
CAM_ADAPTER_SCRIPT = Path(os.getenv("CNC_CAM_ADAPTER_SCRIPT", APP_ROOT / "cam" / "freecad_adapter.py"))
MAX_UPLOAD_BYTES = int(os.getenv("CNC_MAX_UPLOAD_MB", "200")) * 1024 * 1024
PUBLIC_BASE_URL = os.getenv("CNC_PUBLIC_BASE_URL", "").rstrip("/")
MEAS_API_BASE_URL = os.getenv("CNC_MEAS_API_BASE_URL", "").strip().rstrip("/")
HARNESS_ONLY_MODE = os.getenv("CNC_HARNESS_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
MEAS_TIMEOUT_SECONDS = max(float(os.getenv("CNC_MEAS_TIMEOUT_SECONDS", "600")), 1.0)
BENCHMARK_REPORT_PATH = Path(os.getenv(
    "CNC_BENCHMARK_REPORT", STORAGE_ROOT.parent / "benchmark-report.json",
))
CAM_STREAM_LOCK = threading.Lock()
CAM_STREAMING_JOBS: set[str] = set()
AI_REVIEW_LOCK = threading.Lock()
AGENT_PERCEPTION_LOCK = threading.Lock()
AGENT_PERCEIVING_JOBS: set[str] = set()
AGENT_TRIAL_LOCK = threading.Lock()
AGENT_TRIALING_JOBS: set[str] = set()
L32_PROFILE_REVIEW_LOCK = threading.Lock()
L32_PROFILE_REVIEWING_JOBS: set[str] = set()
L32_AGENT_VALIDATION_LOCK = threading.Lock()
L32_AGENT_VALIDATING_JOBS: set[str] = set()
L32_ROLLING_LOOP_LOCK = threading.Lock()
L32_ROLLING_LOOP_JOBS: set[str] = set()
L32_OPERATION_TRIAL_LOCK = threading.Lock()
L32_OPERATION_TRIALING_JOBS: set[str] = set()
L32_AUTONOMOUS_PROCESS_LOCK = threading.Lock()
L32_AUTONOMOUS_PROCESS_JOBS: set[str] = set()
L32_MATERIAL_SNAPSHOT_LOCK = threading.Lock()
AI_REVIEWING_JOBS: set[str] = set()
JOB_EVENT_CONDITION = threading.Condition()
JOB_EVENT_LOGS: dict[str, list[dict[str, object]]] = {}
TOOL_INVENTORY_LOCK = threading.Lock()


def publish_job_event(job_id: str, stage: str, message: str, percent: float, **details: object) -> None:
    with JOB_EVENT_CONDITION:
        events = JOB_EVENT_LOGS.setdefault(job_id, [])
        event = build_agent_event(
            sequence=len(events) + 1,
            stage=stage,
            message=message,
            percent=percent,
            created_at=utc_now(),
            details={key: value for key, value in details.items() if value is not None},
        )
        events.append(event)
        try:
            write_json(job_directory(job_id) / "planning-events.json", events)
        except OSError:
            # Progress delivery must continue even if persistence temporarily fails.
            pass
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
    # Readers and writers run in different FastAPI worker threads. Writing
    # directly to the destination briefly truncates it to zero bytes, so a
    # concurrent material-snapshot request can observe invalid JSON. Write a
    # complete sibling file first and atomically replace the destination.
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_job_events(job_id: str) -> list[dict[str, object]]:
    path = job_directory(job_id) / "planning-events.json"
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def cached_preview_signature(kind: str, source: Path, payload: object) -> str:
    """Identify an exact preview by source revision and all geometric inputs."""
    source_revision = source.stat().st_mtime_ns if source.is_file() else 0
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{kind}:{source_revision}:".encode("utf-8") + serialized.encode("utf-8")).hexdigest()


def read_cached_preview(path: Path, signature: str) -> object | None:
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("signature") == signature:
            return cached["result"]
    except (OSError, ValueError, TypeError, KeyError):
        path.unlink(missing_ok=True)
    return None


def write_cached_preview(path: Path, signature: str, result: object) -> None:
    write_json(path, {"schema_version": "1.0.0", "signature": signature, "result": result})


def persist_rotational_analysis(
    directory: Path, job: JobResponse, analysis: GeometryAnalysis,
) -> RotationalFeatureAnalysis | None:
    path = directory / "rotational-features.json"
    is_l32 = job.device_id == "citizen-cincom-l32" or "citizen cincom l32" in job.machine.lower()
    if not is_l32:
        path.unlink(missing_ok=True)
        return None
    result = infer_rotational_features(analysis)
    for profile in result.profiles:
        raw_decision = analysis.rotational_profile_decisions.get(profile.id)
        if profile.review_state == "ai_provisional" and raw_decision:
            profile.provisional_decision = AIProvisionalProfileDecision.model_validate(raw_decision)
    accepted_axis_ids = {
        profile.axis_id for profile in result.profiles
        if profile.review_state == "accepted"
    }
    provisional_axis_ids = {
        profile.axis_id for profile in result.profiles
        if profile.review_state == "ai_provisional"
    }
    for axis in result.axes:
        if axis.id in accepted_axis_ids:
            axis.review_state = "accepted"
            axis.review_reasons = []
        elif axis.id in provisional_axis_ids:
            axis.review_state = "ai_provisional"
            if "AI provisional axis authorization for DRAFT-only planning" not in axis.review_reasons:
                axis.review_reasons.append("AI provisional axis authorization for DRAFT-only planning")
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
    job = JobResponse.model_validate_json(metadata_path.read_text(encoding="utf-8"))
    if job.analysis and "planar_machining_features" not in job.analysis.model_fields_set:
        job.analysis.planar_machining_features = recognize_planar_machining_features(
            job.analysis,
            job.analysis.prismatic_features,
        )
    return job


def save_job(directory: Path, job: JobResponse) -> None:
    write_json(directory / "job.json", job.model_dump(mode="json"))


def cam_plan_fingerprint(job: JobResponse) -> str:
    """Bind archived CAM evidence to the exact plan and model selection."""
    payload = {
        "plan": job.plan.model_dump(mode="json") if job.plan else None,
        "material": job.material,
        "machine": job.machine,
        "device_id": job.device_id,
        "machine_configuration_hash": job.machine_configuration_hash,
        "selected_solid_index": job.analysis.topology.get("selected_solid_index") if job.analysis else None,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def archive_cam_plan_fingerprint(directory: Path, job: JobResponse) -> None:
    write_json(directory / "cam-manifest.json", {
        "schema_version": "1.0.0",
        "plan_sha256": cam_plan_fingerprint(job),
        "created_at": utc_now(),
    })


def invalidate_cam_artifacts(directory: Path) -> None:
    for filename in (
        "cam.FCStd", "program.nc", "toolpath.json", "verification.json",
        "simulation.json", "collision.json", "remediation.json", "remediation-history.json", "ai-plan.json", "cam-manifest.json",
        "agent-execution.json", "agent-remediation.json",
        "agent-operation-trial.json",
        "agent-l32-operation-trial.json",
        "agent-l32-operation-candidates.json",
        "agent-l32-auto-repair.json",
        "agent-l32-rolling-loop.json",
        "agent-world-model.json", "agent-orchestrator.json",
        "turning-toolpath-ir.json", "turning-simulation.json", "turning-verification.json", "turning-thread-verification.json", "turning-reachability.json", "turning-draft.json",
        "turning-transfer-ir.json", "turning-transfer-draft.json",
        "turning-backside-ir.json", "turning-backside-draft.json",
        "turning-whole-program-ir.json", "turning-whole-program-draft.json",
        "turning-whole-program-timeline.json", "turning-continuous-simulation.json",
        "turning-inner-bore-chain-ir.json", "turning-inner-bore-chain-draft.json",
        "boring-reachability.json", "axial-drilling-review.json", "grooving-review.json",
    ):
        (directory / filename).unlink(missing_ok=True)
    for pattern in (
        "program-*.nc", "camotics-*.stl", "*.camotics", "turning-draft-cache-*.json",
        "l32-preview-cache-*.json",
    ):
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
        "planning_entry": "deepseek_harness" if HARNESS_ONLY_MODE else "legacy_api_and_harness",
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


@app.get("/api/v1/machines/l32/catalog-reference/viii-u30b-u151b")
def get_l32_viii_live_tool_catalog_reference() -> dict[str, object]:
    return l32_viii_live_tool_catalog_reference()


@app.get(
    "/api/v1/jobs/{job_id}/l32/catalog-back-pocket/{feature_id}/draft",
    response_model=IndexedPocketDraft,
)
def get_l32_catalog_back_pocket_draft(job_id: str, feature_id: str) -> IndexedPocketDraft:
    """Read-only geometric draft for the catalog U151B scenario, never NC."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None:
        raise HTTPException(status_code=409, detail="L32 geometry analysis is required")
    feature = next((item for item in job.analysis.prismatic_features if item.id == feature_id), None)
    if feature is None:
        raise HTTPException(status_code=404, detail="Pocket feature not found")
    try:
        draft = build_indexed_back_pocket_draft(job.analysis, feature)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    snapshot_path = job_directory(job_id) / "machine-configuration.json"
    if job.machine_instance_id and snapshot_path.is_file():
        snapshot = load_machine_snapshot(snapshot_path)
        draft.bound_machine_has_required_module = (
            draft.required_module in snapshot.instance.installed_modules
            and snapshot.instance.id == job.machine_instance_id
            and snapshot.configuration_hash == job.machine_configuration_hash
        )
    return draft


@app.get(
    "/api/v1/jobs/{job_id}/l32/catalog-back-pocket/{feature_id}/sweep-check",
    response_model=IndexedPocketSweepCheck,
)
def check_l32_catalog_back_pocket_sweep(job_id: str, feature_id: str) -> IndexedPocketSweepCheck:
    """Compare the actual OCC cutter sweep to the original STEP solid."""
    draft = get_l32_catalog_back_pocket_draft(job_id, feature_id)
    return _run_l32_pocket_sweep(job_id, draft)


def _run_l32_pocket_sweep(
    job_id: str, draft: IndexedPocketDraft,
) -> IndexedPocketSweepCheck:
    """Run an exact pocket sweep for either the default or a sandbox candidate draft."""
    job = load_job(job_id)
    source = job_directory(job_id) / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp", ".step"}:
        raise HTTPException(status_code=404, detail="Original STEP source is unavailable")
    cache_signature = cached_preview_signature(
        "l32-pocket-sweep-v1", source, draft.model_dump(mode="json"),
    )
    cache_path = job_directory(job_id) / f"l32-preview-cache-pocket-{cache_signature[:20]}.json"
    cached = read_cached_preview(cache_path, cache_signature)
    if isinstance(cached, dict):
        return IndexedPocketSweepCheck.model_validate(cached)
    try:
        completed = run_freecad_adapter(
            FREECAD_CMD,
            APP_ROOT / "cam" / "l32_pocket_sweep.py",
            [source, draft.model_dump_json()],
            timeout_seconds=120,
        )
        line = next(
            item.split("CNC_POCKET_SWEEP ", 1)[1]
            for item in completed.stdout.splitlines()
            if "CNC_POCKET_SWEEP " in item
        )
        result = json.loads(line)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="Exact pocket sweep timed out") from error
    except (OSError, subprocess.CalledProcessError, StopIteration, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"Exact pocket sweep failed: {error}") from error
    contact = float(result["summed_target_contact_mm3"])
    residual = float(result["remaining_pocket_region_mm3"])
    status = (
        "overcut" if contact > 0.0001
        else "residual" if residual > 0.0001
        else "within_geometric_tolerance"
    )
    response = IndexedPocketSweepCheck(
        feature_id=draft.feature_id,
        bound_machine_has_required_module=draft.bound_machine_has_required_module,
        pocket_region_status=status,
        target_material_inside_pocket_region_mm3=result["target_material_inside_pocket_region_mm3"],
        summed_target_contact_mm3=contact,
        remaining_pocket_region_mm3=residual,
        removed_pocket_region_mm3=round(float(result["region_volume_mm3"]) - residual, 6),
        stages=result["stages"],
    )
    write_cached_preview(cache_path, cache_signature, response.model_dump(mode="json"))
    return response


@app.get("/api/v1/jobs/{job_id}/l32/catalog-ear-geometry")
def get_l32_catalog_ear_geometry(job_id: str) -> dict[str, object]:
    """Expose exact radial side-face boundaries without changing the job."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None:
        raise HTTPException(status_code=409, detail="L32 geometry analysis is required")
    source = job_directory(job_id) / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp", ".step"}:
        raise HTTPException(status_code=404, detail="Original STEP source is unavailable")
    cache_signature = cached_preview_signature(
        "l32-exact-side-faces-v1", source,
        [face.model_dump(mode="json") for face in job.analysis.planar_features],
    )
    cache_path = job_directory(job_id) / "l32-preview-cache-side-faces.json"
    cached = read_cached_preview(cache_path, cache_signature)
    if isinstance(cached, dict):
        return cached
    try:
        completed = run_freecad_adapter(
            FREECAD_CMD, APP_ROOT / "cam" / "l32_exact_sections.py",
            [source], timeout_seconds=120,
        )
        line = next(
            item.split("CNC_EXACT_SECTIONS ", 1)[1]
            for item in completed.stdout.splitlines()
            if "CNC_EXACT_SECTIONS " in item
        )
        result = json.loads(line)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="Exact side-face extraction timed out") from error
    except (OSError, subprocess.CalledProcessError, StopIteration, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"Exact side-face extraction failed: {error}") from error
    planar_by_index = {
        face.source_face_index: face
        for face in job.analysis.planar_features if face.source_face_index is not None
    }
    faces = []
    for face in result["broad_side_faces"]:
        analyzed = planar_by_index.get(face["face_number"])
        if analyzed is None or abs(analyzed.normal.y) < 0.999:
            continue
        if abs(analyzed.area - face["area_mm2"]) > max(0.01, analyzed.area * 0.001):
            continue
        faces.append({**face, "analysis_face_id": analyzed.id})
    payload = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "source": "original_step_exact_faces",
        "required_module": "U30B",
        "reference_only": True,
        "nc_generated": False,
        "side_faces": faces,
    }
    write_cached_preview(cache_path, cache_signature, payload)
    return payload


@app.get("/api/v1/jobs/{job_id}/l32/catalog-ear-toolpaths")
def get_l32_catalog_ear_toolpaths(job_id: str) -> dict[str, object]:
    """Build inset side-milling centerlines; no material or holder claim."""
    job = load_job(job_id)
    if job.plan is None or job.plan.stock.get("type") != "round_bar":
        raise HTTPException(status_code=409, detail="L32 round-bar process plan is required")
    geometry = get_l32_catalog_ear_geometry(job_id)
    faces = geometry["side_faces"]
    if not faces:
        raise HTTPException(status_code=422, detail="No exact radial side faces matched the analysis")
    try:
        drafts = [
            build_l32_side_mill_draft(
                face, stock_radius_mm=float(job.plan.stock["diameter_mm"])/2,
            ).model_dump(mode="json")
            for face in faces
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    snapshot_path = job_directory(job_id) / "machine-configuration.json"
    has_module = None
    if job.machine_instance_id and snapshot_path.is_file():
        snapshot = load_machine_snapshot(snapshot_path)
        has_module = (
            "U30B" in snapshot.instance.installed_modules
            and snapshot.instance.id == job.machine_instance_id
            and snapshot.configuration_hash == job.machine_configuration_hash
        )
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "reference_only": True,
        "nc_generated": False,
        "material_sweep_verified": False,
        "bound_machine_has_required_module": has_module,
        "side_drafts": drafts,
    }


@app.get("/api/v1/jobs/{job_id}/l32/catalog-ear-sweep-check")
def check_l32_catalog_ear_sweep(job_id: str) -> dict[str, object]:
    """Run every side-milling feed through OCC against the original target."""
    drafts = get_l32_catalog_ear_toolpaths(job_id)
    return _check_l32_reference_sweep(job_id,drafts["side_drafts"],drafts["bound_machine_has_required_module"])


def _check_l32_reference_sweep(
    job_id: str, toolpaths: list[dict[str, object]], has_module: bool | None,
) -> dict[str, object]:
    job = load_job(job_id)
    source = job_directory(job_id) / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp", ".step"}:
        raise HTTPException(status_code=404, detail="Original STEP source is unavailable")
    cache_signature = cached_preview_signature(
        "l32-reference-sweep-v2", source,
        {"toolpaths": toolpaths, "bound_machine_has_required_module": has_module},
    )
    cache_path = job_directory(job_id) / f"l32-preview-cache-sweep-{cache_signature[:20]}.json"
    cached = read_cached_preview(cache_path, cache_signature)
    if isinstance(cached, dict):
        return cached
    try:
        completed = run_freecad_adapter(
            FREECAD_CMD, APP_ROOT / "cam" / "l32_side_sweep.py",
            [source, json.dumps(toolpaths, separators=(",", ":"))],
            timeout_seconds=120,
        )
        line = next(
            item.split("CNC_SIDE_SWEEP ", 1)[1]
            for item in completed.stdout.splitlines()
            if "CNC_SIDE_SWEEP " in item
        )
        result = json.loads(line)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="Exact side sweep timed out") from error
    except (OSError, subprocess.CalledProcessError, StopIteration, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"Exact side sweep failed: {error}") from error
    checks = result["checks"]
    payload = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "reference_only": True,
        "nc_generated": False,
        "bound_machine_has_required_module": has_module,
        "target_gouge_check_passed": bool(result["target_solid_valid"])
        and len(checks) == len(toolpaths)
        and all(
            item["contacting_segments"] == 0
            and item["summed_target_contact_mm3"] <= 0.000001
            and item["analysis_face_id"] == toolpaths[index]["analysis_face_id"]
            for index,item in enumerate(checks)
        ),
        "whole_part_material_verified": False,
        "initial_stock_volume_mm3": result.get("initial_stock_volume_mm3"),
        "remaining_stock_volume_mm3": result.get("remaining_stock_volume_mm3"),
        "removed_stock_volume_mm3": result.get("removed_stock_volume_mm3"),
        "missing_target_volume_mm3": result.get("missing_target_volume_mm3"),
        "machining_region_x_mm": result.get("machining_region_x_mm"),
        "initial_excess_region_mm3": result.get("initial_excess_region_mm3"),
        "remaining_excess_region_mm3": result.get("remaining_excess_region_mm3"),
        "checks": checks,
    }
    write_cached_preview(cache_path, cache_signature, payload)
    return payload


@app.get("/api/v1/jobs/{job_id}/l32/catalog-exterior-toolpaths")
def get_l32_catalog_exterior_toolpaths(job_id: str) -> dict[str, object]:
    """Clear the rear exterior only after exact 3D sweep verification."""
    job = load_job(job_id)
    if job.plan is None or job.plan.stock.get("type") != "round_bar":
        raise HTTPException(status_code=409, detail="L32 round-bar process plan is required")
    geometry = get_l32_catalog_ear_geometry(job_id)
    faces = geometry["side_faces"]
    if len(faces) != 2 or {int(face["normal_y"]) for face in faces} != {-1,1}:
        raise HTTPException(status_code=422, detail="Two opposing exact radial faces are required")
    try:
        drafts = [
            build_l32_exterior_clear_draft(
                face,
                stock_radius_mm=float(job.plan.stock["diameter_mm"])/2,
                access_sign=1 if face["normal_y"] > 0 else -1,
            ).model_dump(mode="json")
            for face in faces
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    snapshot_path = job_directory(job_id) / "machine-configuration.json"
    has_module = None
    if job.machine_instance_id and snapshot_path.is_file():
        snapshot = load_machine_snapshot(snapshot_path)
        has_module = (
            "U30B" in snapshot.instance.installed_modules
            and snapshot.instance.id == job.machine_instance_id
            and snapshot.configuration_hash == job.machine_configuration_hash
        )
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "reference_only": True,
        "nc_generated": False,
        "material_sweep_verified": False,
        "bound_machine_has_required_module": has_module,
        "exterior_drafts": drafts,
    }


@app.get("/api/v1/jobs/{job_id}/l32/catalog-exterior-sweep-check")
def check_l32_catalog_exterior_sweep(job_id: str) -> dict[str, object]:
    drafts = get_l32_catalog_exterior_toolpaths(job_id)
    return _check_l32_reference_sweep(job_id,drafts["exterior_drafts"],drafts["bound_machine_has_required_module"])


@app.get("/api/v1/jobs/{job_id}/l32/material-snapshots")
def get_l32_material_snapshots(job_id: str) -> dict[str, object]:
    """Generate clean, cumulative OCC solids for geometric L32 milling playback."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.plan is None:
        raise HTTPException(status_code=409, detail="L32 process plan is required")
    directory = job_directory(job_id)
    source = directory / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp", ".step"}:
        raise HTTPException(status_code=404, detail="Original STEP source is unavailable")

    operations = [item for setup in job.plan.setups for item in setup.operations]
    rotational_path = directory / "rotational-features.json"
    if not rotational_path.is_file():
        raise HTTPException(status_code=404, detail="Exact rotational feature analysis is unavailable")
    rotational = RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8"))
    if not rotational.axes:
        raise HTTPException(status_code=422, detail="No rotational axis is available")
    source_profile = next(
        (profile for profile in rotational.profiles if profile.id == job.plan.stock.get("rotational_profile_id")),
        None,
    )
    if source_profile is None or source_profile.review_state != "accepted":
        raise HTTPException(status_code=422, detail="Accepted outer rotational profile is unavailable")
    profile_min = min(point.z for point in source_profile.points)
    profile_max = max(point.z for point in source_profile.points)
    region = job.plan.stock.get("nonrotational_region_z_mm")
    explicit_nonrotational_region = isinstance(region, list) and len(region) == 2
    if explicit_nonrotational_region:
        region_min, region_max = sorted((float(region[0]), float(region[1])))
    else:
        # A fully rotational part legitimately has no nonrotational region.
        # Its accepted exact profile is the material snapshot axial envelope.
        region_min, region_max = profile_min, profile_max
    face_operation = next((
        item for item in operations
        if item.enabled is not False and item.type == "turn_facing" and item.workpiece_side == "front"
    ), None)
    rough_operation = next((
        item for item in operations
        if item.enabled is not False and item.type == "turn_od_roughing" and item.workpiece_side == "front"
    ), None)
    planned_front_minima = [
        float(item.parameters["profile_z_min_mm"])
        for item in operations
        if item.enabled is not False and item.workpiece_side == "front"
        and item.type in {"turn_od_roughing", "turn_od_finishing"}
        and isinstance(item.parameters.get("profile_z_min_mm"), (int, float))
    ]
    front_min = max(
        profile_min,
        min(planned_front_minima) if planned_front_minima else (
            region_max if explicit_nonrotational_region else profile_min
        ),
    )
    front_max = float(face_operation.parameters.get("face_z_mm", max(point.z for point in source_profile.points))) if face_operation else max(point.z for point in source_profile.points)
    if front_max <= front_min:
        raise HTTPException(status_code=422, detail="Front turning region has no axial extent")
    stock_radius = float(job.plan.stock["diameter_mm"]) / 2
    rough_allowance = float(rough_operation.parameters.get("radial_allowance_mm", 0.3)) if rough_operation else 0.3
    front_profile_radius = max(point.radius for point in source_profile.points if front_min - 1e-6 <= point.z <= front_max + 1e-6)
    grooves: dict[str, dict[str, float]] = {}
    pockets: dict[str, dict[str, object]] = {}
    cutoffs: dict[str, dict[str, float]] = {}
    back_faces: dict[str, dict[str, float]] = {}
    stages: list[dict[str, object]] = []
    for operation in operations:
        if operation.enabled is False:
            continue
        if operation.type == "turn_facing" and operation.workpiece_side == "front":
            stages.append({"operation_id": operation.id, "kind": "face", "rough": False})
        elif operation.type in {"turn_od_roughing", "turn_od_finishing"} and operation.workpiece_side == "front":
            stages.append({"operation_id": operation.id, "kind": "front", "rough": operation.type == "turn_od_roughing"})
        elif operation.type == "turn_grooving" and operation.workpiece_side == "front":
            feature = next((item for item in rotational.features if item.id in operation.feature_ids and item.kind == "external_groove_candidate"), None)
            if feature:
                try:
                    draft = build_front_groove_geometry_draft(
                        source_profile, feature,
                        stock_radius_mm=stock_radius,
                        actual_planned_tool_width_mm=float(operation.tool.cutting_width_mm or 0),
                    )
                except (TypeError, ValueError):
                    # An unconfigured groove must not prevent other operations
                    # from obtaining their independently valid stock snapshots.
                    continue
                grooves[operation.id] = {
                    "minimum": min(strip.z_min_mm for strip in draft.strips),
                    "maximum": max(strip.z_max_mm for strip in draft.strips),
                    "radius": max(feature.radius_start, feature.radius_end) + feature.depth_mm,
                    "floor_radius": min(feature.radius_start, feature.radius_end),
                }
                stages.append({"operation_id": operation.id, "kind": "groove", "rough": False})
        elif operation.type == "turn_cutoff":
            try:
                width = float(operation.parameters["cutting_width_mm"])
                center = float(operation.parameters["z_mm"])
            except (KeyError, TypeError, ValueError):
                continue
            lower, upper = center - width / 2, center + width / 2
            # The sacrificial kerf must remain behind the retained STEP part.
            # An invalid or overlapping setup cannot be represented as verified IPW.
            if not all(isfinite(value) for value in (width, center, lower, upper)) or width <= 0 or upper > region_min + 1e-6:
                continue
            cutoffs[operation.id] = {"minimum": lower, "maximum": upper}
            stages.append({"operation_id": operation.id, "kind": "cutoff", "rough": False})
        elif operation.type == "live_tool_contour_roughing":
            stages.append({"operation_id": operation.id, "kind": "exterior", "rough": True})
        elif operation.type == "live_tool_contour_finishing":
            stages.append({"operation_id": operation.id, "kind": "exterior", "rough": False})
        elif operation.type == "back_live_face_finishing":
            finished = float(operation.parameters.get(
                "finished_back_datum_z_mm", job.plan.stock.get("finished_back_z_mm"),
            ))
            allowance = float(operation.parameters.get("stock_allowance_mm", 0))
            if allowance > 0:
                back_faces[operation.id] = {
                    "minimum": finished - allowance,
                    "maximum": finished,
                }
                stages.append({"operation_id": operation.id, "kind": "back_face", "rough": False})
        elif operation.type in {"pocket_roughing", "pocket_finishing"}:
            feature_id = next((item for item in operation.feature_ids if item.startswith("MF-")), None)
            if feature_id:
                pockets.setdefault(
                    feature_id,
                    get_l32_catalog_back_pocket_draft(job_id, feature_id).model_dump(mode="json"),
                )
                stages.append({
                    "operation_id": operation.id,
                    "kind": "pocket",
                    "rough": operation.type == "pocket_roughing",
                    "feature_id": feature_id,
                })

    if not stages:
        return {"schema_version": "1.0.0", "operations": []}
    axis = rotational.axes[0]
    context = {
        "axis_origin": axis.origin.model_dump(mode="json"),
        "axis_direction": axis.direction.model_dump(mode="json"),
        "stock_radius": stock_radius,
        "region_min": region_min,
        "region_max": region_max,
        "front_region_min": front_min,
        "front_region_max": front_max,
        "front_rough_radius": min(stock_radius, front_profile_radius + rough_allowance),
        "front_floor_radius": min(
            (point.radius for point in source_profile.points
             if front_min - 1e-6 <= point.z <= front_max + 1e-6
             and not any(groove["minimum"] <= point.z <= groove["maximum"] for groove in grooves.values())),
            default=min(point.radius for point in source_profile.points if front_min - 1e-6 <= point.z <= front_max + 1e-6),
        ),
        "face_overhang": float(job.plan.stock.get("allowance_mm", {}).get("axial", 2.0)),
        "grooves": grooves,
        "cutoffs": cutoffs,
        "back_faces": back_faces,
        "pockets": pockets,
    }
    stages_json = json.dumps({"stages": stages, "context": context}, separators=(",", ":"))
    signature = hashlib.sha256(
        f"material-binary-v12:{source.stat().st_mtime_ns}:".encode("utf-8") + stages_json.encode("utf-8")
    ).hexdigest()
    manifest_path = directory / "l32-material-snapshots.json"
    stages_path = directory / "l32-material-stages.json"
    with L32_MATERIAL_SNAPSHOT_LOCK:
        if manifest_path.is_file():
            try:
                cached = json.loads(manifest_path.read_text(encoding="utf-8"))
                cached_files = [
                    name for item in cached.get("operations", []) for name in item.get("files", [])
                ]
                if (
                    cached.get("signature") == signature
                    and len(cached.get("operations", [])) == len(stages)
                    and all((directory / name).is_file() and (directory / name).stat().st_size > 84 for name in cached_files)
                ):
                    return cached
            except (OSError, ValueError, TypeError):
                pass
        try:
            write_json(stages_path, {"stages": stages, "context": context})
            with tempfile.TemporaryDirectory(prefix="l32-material-build-", dir=directory) as temporary:
                completed = run_freecad_adapter(
                    FREECAD_CMD,
                    APP_ROOT / "cam" / "l32_material_snapshots.py",
                    [source, temporary, stages_path],
                    timeout_seconds=300,
                )
                line = next(
                    item.split("CNC_L32_MATERIAL ", 1)[1]
                    for item in completed.stdout.splitlines()
                    if "CNC_L32_MATERIAL " in item
                )
                result = json.loads(line)
                expected_ids = [stage["operation_id"] for stage in stages]
                if [item["operation_id"] for item in result["operations"]] != expected_ids:
                    raise ValueError("Snapshot operations do not match the process plan")
                names = [name for item in result["operations"] for name in item["files"]]
                invalid_names = [
                    name for name in names if Path(name).name != name
                    or not name.startswith("l32-material-") or not name.endswith(".stl")
                    or not (Path(temporary) / name).is_file()
                    or (Path(temporary) / name).stat().st_size <= 84
                ]
                if not names or invalid_names:
                    raise ValueError(f"Material snapshots missing or invalid: {invalid_names[:5]}")
                for name in names:
                    (Path(temporary) / name).replace(directory / name)
        except subprocess.TimeoutExpired as error:
            raise HTTPException(status_code=504, detail="L32 material snapshots timed out") from error
        except (OSError, subprocess.CalledProcessError, StopIteration, ValueError, KeyError) as error:
            raise HTTPException(status_code=502, detail=f"L32 material snapshots failed: {error}") from error
        payload = {"schema_version": "1.0.0", "signature": signature, **result}
        write_json(manifest_path, payload)
        return payload


@app.get("/api/v1/jobs/{job_id}/l32/front-groove-geometry")
def get_l32_front_groove_geometry(job_id: str) -> dict[str, object]:
    """Expose the exact-profile groove and the currently incompatible tool width."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.plan is None:
        raise HTTPException(status_code=409, detail="L32 process plan is required")
    source = job_directory(job_id) / "rotational-features.json"
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Exact rotational feature analysis is unavailable")
    analysis = RotationalFeatureAnalysis.model_validate_json(source.read_text(encoding="utf-8"))
    operations = [op for setup in job.plan.setups for op in setup.operations]
    groove_operations = [op for op in operations if op.type == "turn_grooving" and op.workpiece_side == "front"]
    physical_tools = _load_tool_inventory(job.machine_instance_id) if job.machine_instance_id else []
    drafts = []
    for operation in groove_operations:
        features = [
            item for item in analysis.features
            if item.id in operation.feature_ids and item.kind == "external_groove_candidate"
        ]
        for feature in features:
            profile = next((item for item in analysis.profiles if item.id == feature.profile_id),None)
            if profile is None:
                continue
            if job.plan.stock.get("rotational_profile_id") != profile.id:
                raise HTTPException(status_code=409, detail="Groove profile differs from the bound process-plan profile")
            try:
                draft = build_front_groove_geometry_draft(
                    profile,feature,
                    stock_radius_mm=float(job.plan.stock["diameter_mm"])/2,
                    actual_planned_tool_width_mm=float(operation.tool.cutting_width_mm or 0),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from error
            drafts.append({
                "operation_id":operation.id,"operation_enabled":operation.enabled,
                "draft":draft.model_dump(mode="json"),
                "physical_width_candidates": [
                    {
                        "inventory_id":tool.inventory_id,
                        "measured_cutting_width_mm":tool.measured_cutting_width_mm,
                        "verification_state":tool.verification_state,
                        "toolpath_approved":False,
                    }
                    for tool in physical_tools if physical_tool_fit_for_groove(tool,feature.width_mm)
                ],
            })
    return {
        "schema_version":"1.0.0","job_id":job_id,"reference_only":True,
        "nc_generated":False,"material_sweep_verified":False,"grooves":drafts,
    }


@app.get("/api/v1/jobs/{job_id}/l32/front-groove-sweep-check")
def check_l32_front_groove_sweep(
    job_id: str, operation_id: str | None = None,
) -> dict[str, object]:
    geometry = get_l32_front_groove_geometry(job_id)
    matching = [
        item for item in geometry["grooves"]
        if operation_id is None or item["operation_id"] == operation_id
    ]
    if operation_id is not None and not matching:
        raise HTTPException(status_code=404, detail="Front groove operation was not found")
    if len(matching) != 1:
        raise HTTPException(status_code=422, detail="Exactly one exact front external groove is required")
    job = load_job(job_id)
    source = job_directory(job_id) / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp",".step"}:
        raise HTTPException(status_code=404, detail="Original STEP source is unavailable")
    analysis_path = job_directory(job_id) / "rotational-features.json"
    analysis = RotationalFeatureAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))
    selected = matching[0]
    draft = selected["draft"]
    profile = next(item for item in analysis.profiles if item.id == draft["profile_id"])
    axis = next(item for item in analysis.axes if item.id == profile.axis_id)
    if axis.review_state != "accepted":
        raise HTTPException(status_code=409, detail="Groove rotation axis has not been accepted")
    cache_signature = cached_preview_signature(
        "l32-front-groove-sweep-v1", source,
        {"draft": draft, "axis": axis.model_dump(mode="json")},
    )
    cache_path = job_directory(job_id) / f"l32-preview-cache-groove-{cache_signature[:20]}.json"
    cached = read_cached_preview(cache_path, cache_signature)
    if isinstance(cached, dict):
        return cached
    try:
        completed = run_freecad_adapter(
            FREECAD_CMD, APP_ROOT / "cam" / "l32_front_groove_sweep.py",
            [source,json.dumps(draft,separators=(",",":")),json.dumps(axis.model_dump(mode="json"),separators=(",",":"))],
            timeout_seconds=120,
        )
        line = next(
            item.split("CNC_FRONT_GROOVE_SWEEP ",1)[1]
            for item in completed.stdout.splitlines() if "CNC_FRONT_GROOVE_SWEEP " in item
        )
        result = json.loads(line)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="Front groove OCC sweep timed out") from error
    except (OSError, subprocess.CalledProcessError, StopIteration, ValueError) as error:
        raise HTTPException(status_code=502, detail=f"Front groove OCC sweep failed: {error}") from error
    payload = {
        "schema_version":"1.0.0","job_id":job_id,"reference_only":True,"nc_generated":False,
        "operation_id":selected["operation_id"],
        "actual_planned_tool_fits_floor":draft["actual_tool_fits_floor"],
        "target_gouge_check_passed":bool(result["target_solid_valid"] and result["remaining_stock_valid"]
            and result["summed_target_contact_mm3"] <= 0.000001
            and result["missing_target_volume_mm3"] <= 0.000001),
        "whole_part_material_verified":False,"check":result,
    }
    write_cached_preview(cache_path, cache_signature, payload)
    return payload


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


def _tool_inventory_path(instance_id: str) -> Path:
    # Reuse the machine-identity validation before forming a storage path.
    machine_instance_path(instance_id)
    return STORAGE_ROOT / ".tool-inventory" / f"{instance_id}.json"


def _load_tool_inventory(instance_id: str) -> list[ToolInventoryRecord]:
    load_machine_snapshot(machine_instance_path(instance_id))
    path = _tool_inventory_path(instance_id)
    if not path.is_file():
        return []
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return [ToolInventoryRecord.model_validate(value) for value in values]
    except (OSError, ValueError, TypeError) as error:
        raise HTTPException(status_code=500, detail="Stored tool inventory is invalid") from error


@app.get("/api/v1/machines/l32/instances/{instance_id}/tools")
def list_l32_physical_tools(instance_id: str) -> dict[str, object]:
    records = _load_tool_inventory(instance_id)
    return {
        "schema_version": "1.0.0",
        "machine_instance_id": instance_id,
        "tools": [record.model_dump(mode="json") for record in records],
    }


@app.post("/api/v1/machines/l32/instances/{instance_id}/tools", status_code=201)
def create_l32_physical_tool(instance_id: str, request: ToolInventoryInput) -> ToolInventoryRecord:
    with TOOL_INVENTORY_LOCK:
        records = _load_tool_inventory(instance_id)
        if any(record.inventory_id == request.inventory_id for record in records):
            raise HTTPException(status_code=409, detail="Physical tool inventory ID already exists")
        try:
            catalog_tool = get_tool(request.catalog_tool_id) if request.catalog_tool_id else None
            record = record_physical_tool(instance_id, request, catalog_tool)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        path = _tool_inventory_path(instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, [*map(lambda item: item.model_dump(mode="json"), records), record.model_dump(mode="json")])
        return record


@app.put("/api/v1/machines/l32/instances/{instance_id}/tools/{inventory_id}")
def update_l32_physical_tool(
    instance_id: str, inventory_id: str, request: ToolInventoryInput,
) -> ToolInventoryRecord:
    if inventory_id != request.inventory_id:
        raise HTTPException(status_code=422, detail="Physical tool inventory ID cannot change")
    with TOOL_INVENTORY_LOCK:
        records = _load_tool_inventory(instance_id)
        index = next((index for index,item in enumerate(records) if item.inventory_id == inventory_id),None)
        if index is None:
            raise HTTPException(status_code=404, detail="Physical tool not found")
        try:
            catalog_tool = get_tool(request.catalog_tool_id) if request.catalog_tool_id else None
            record = record_physical_tool(instance_id,request,catalog_tool,records[index])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        records[index] = record
        write_json(_tool_inventory_path(instance_id),[item.model_dump(mode="json") for item in records])
        return record


def apply_l32_machine_configuration(
    job: JobResponse,
    snapshot: MachineConfigurationSnapshot,
) -> None:
    """Apply one validated machine snapshot to every capability-gated operation."""
    job.machine_instance_id = snapshot.instance.id
    job.machine_configuration_hash = snapshot.configuration_hash
    if not job.plan:
        return
    job.plan.stock["machine_instance_id"] = snapshot.instance.id
    job.plan.stock["machine_configuration_hash"] = snapshot.configuration_hash
    back_module_available = "back_turning" in snapshot.validation.capabilities
    back_live_tool_available = "back_live_tool_milling" in snapshot.validation.capabilities
    back_turning_enabled = (
        back_module_available
        and job.plan.stock.get("nonrotational_turning_limit_z_mm") is None
    )
    for setup in job.plan.setups:
        for operation in setup.operations:
            if operation.workpiece_side != "back":
                continue
            if operation.type in {"pocket_roughing", "pocket_finishing", "back_live_face_finishing"}:
                operation.enabled = back_live_tool_available
            else:
                operation.enabled = back_turning_enabled and operation.id != "OP60"
            operation.generation_state = "dirty"
    warning = (
        "当前零件背面含非回转结构，原背轴车削工序会误切成品，已保持禁用。"
        if back_module_available and not back_turning_enabled
        else "当前绑定设备实例未确认 back_turning 刀具模块，背面工序保持禁用。"
    )
    legacy_warning = "当前绑定设备实例未确认 back_turning 刀具模块，OP50/OP60 背面工序保持禁用。"
    job.plan.warnings = [item for item in job.plan.warnings if item != legacy_warning]
    if back_turning_enabled:
        job.plan.warnings = [item for item in job.plan.warnings if item != warning]
    elif warning not in job.plan.warnings:
        job.plan.warnings.append(warning)
    redundant_cleanup_warning = "旧版 OP60 清根与前序轮廓加工重复，已保持禁用以防空走刀或重复过切。"
    if any(
        item.id == "OP60" for setup in job.plan.setups for item in setup.operations
    ):
        if redundant_cleanup_warning not in job.plan.warnings:
            job.plan.warnings.append(redundant_cleanup_warning)
    else:
        job.plan.warnings = [
            item for item in job.plan.warnings if item != redundant_cleanup_warning
        ]
    if job.analysis:
        job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
        job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
        job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)


def reapply_bound_l32_machine_configuration(job: JobResponse, directory: Path) -> None:
    if not job.machine_instance_id or not job.machine_configuration_hash or not job.plan:
        return
    snapshot = load_machine_snapshot(directory / "machine-configuration.json")
    if (
        snapshot.instance.id != job.machine_instance_id
        or snapshot.configuration_hash != job.machine_configuration_hash
    ):
        raise ValueError("bound machine configuration integrity check failed")
    apply_l32_machine_configuration(job, snapshot)


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
    apply_l32_machine_configuration(job, snapshot)

    directory = job_directory(job_id)
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
        request.operation.source == "automatic"
        and (
            "profile_z_min_mm" in request.operation.parameters
            or "profile_z_max_mm" in request.operation.parameters
            or request.operation.parameters.get("cut_direction") == "positive_z"
        )
    ):
        planned = next(
            (
                item for setup in (job.plan.setups if job.plan else []) for item in setup.operations
                if item.id == request.operation.id and item.type == request.operation.type
            ),
            None,
        )
        if planned is None or planned.model_dump(mode="json") != request.operation.model_dump(mode="json"):
            raise HTTPException(
                status_code=409,
                detail="Regional automatic draft must exactly match the stored operation",
            )
        if not planned.enabled:
            raise HTTPException(status_code=409, detail="Regional automatic operation is disabled")
    elif (
        request.operation.type in {"turn_id_roughing", "turn_id_finishing", "turn_grooving"}
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
            raise HTTPException(status_code=409, detail="Reviewed automatic draft must exactly match the stored operation")
        if (
            not planned.enabled
            or planned.parameters.get("engineering_review_status") != "verified_engineer"
        ):
            raise HTTPException(status_code=409, detail="Automatic operation has not completed DRAFT-level engineering review")

    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before draft generation")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Draft request does not use the machine instance bound to this job")
    directory = job_directory(job_id)
    snapshot_path = directory / "machine-configuration.json"
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
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    source = directory / job.filename
    source_revision = source.stat().st_mtime_ns if source.is_file() else 0
    cache_signature = hashlib.sha256(json.dumps({
        "schema": "turning-draft-cache-v1",
        "source_revision": source_revision,
        "machine_configuration_hash": job.machine_configuration_hash,
        "request": request.model_dump(mode="json"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    operation_cache_id = hashlib.sha256(request.operation.id.encode("utf-8")).hexdigest()[:20]
    operation_cache_path = directory / f"turning-draft-cache-{operation_cache_id}.json"
    if operation_cache_path.is_file():
        try:
            cached = json.loads(operation_cache_path.read_text(encoding="utf-8"))
            if cached.get("signature") == cache_signature:
                return TurningDraftResult.model_validate(cached["result"])
        except (OSError, ValueError, TypeError, KeyError):
            operation_cache_path.unlink(missing_ok=True)

    try:
        result = compile_turning_draft(job_id, request, snapshot)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

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
    write_json(operation_cache_path, {
        "schema_version": "1.0.0",
        "signature": cache_signature,
        "operation_id": request.operation.id,
        "result": result.model_dump(mode="json"),
    })
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
    if planned_operation.id == "OP60" and any(
        item.id == "OP58-BACK"
        for setup in job.plan.setups for item in setup.operations
    ):
        raise HTTPException(status_code=409, detail="Legacy OP60 cleanup overlaps the planned backside region")
    if request.operation.model_dump(mode="json") != planned_operation.model_dump(mode="json"):
        raise HTTPException(status_code=422, detail="Submitted backside operation does not match the formal process plan")
    planned_profile_id = str(job.plan.stock.get("rotational_profile_id", ""))
    cutoff_operation = next((
        item for setup in job.plan.setups for item in setup.operations
        if item.id == "OP40" and item.type == "turn_cutoff"
    ), None)
    if request.source_profile_id != planned_profile_id:
        raise HTTPException(status_code=422, detail="Backside source profile does not match the formal process plan")
    expected_back_datum = (
        float(cutoff_operation.parameters.get(
            "finished_back_datum_z_mm", cutoff_operation.parameters.get("z_mm", float("nan")),
        )) if cutoff_operation is not None else float("nan")
    )
    if cutoff_operation is None or abs(request.source_cutoff_z_mm - expected_back_datum) > 1e-6:
        raise HTTPException(status_code=422, detail="Backside cutoff datum does not match the formal process plan")

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
    "/api/v1/jobs/{job_id}/turning/front-chain/draft",
    response_model=FrontChainDraftResult,
)
def generate_turning_front_chain_draft(
    job_id: str, request: FrontChainDraftRequest,
) -> FrontChainDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Front chain requires an L32 process plan")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine before front chain simulation")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Front chain does not use the machine instance bound to this job")
    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        profile = next((
            item for item in rotational.profiles if item.id == request.source_profile_id
        ), None)
        if profile is None:
            raise ValueError("front chain source profile is not available")
        result = compile_front_chain_draft(job_id, request, job.plan, profile, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-front-chain-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-front-chain-draft.json", result.model_dump(mode="json"))
    return result


@app.post(
    "/api/v1/jobs/{job_id}/turning/backside-chain/draft",
    response_model=BacksideChainDraftResult,
)
def generate_turning_backside_chain_draft(
    job_id: str, request: BacksideChainDraftRequest,
) -> BacksideChainDraftResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Backside chain requires an L32 process plan")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine before backside chain simulation")
    if request.machine_instance_id != job.machine_instance_id:
        raise HTTPException(status_code=422, detail="Backside chain does not use the machine instance bound to this job")
    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        profile = next((
            item for item in rotational.profiles if item.id == request.source_profile_id
        ), None)
        if profile is None:
            raise ValueError("backside chain source profile is not available")
        result = compile_backside_chain_draft(job_id, request, job.plan, profile, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-backside-chain-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-backside-chain-draft.json", result.model_dump(mode="json"))
    return result


@app.post(
    "/api/v1/jobs/{job_id}/turning/inner-bore-chain/draft",
    response_model=InnerBoreChainResult,
)
def generate_inner_bore_chain_draft(
    job_id: str, request: InnerBoreChainRequest,
) -> InnerBoreChainResult:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Inner-bore chain requires an L32 process plan")
    if not job.machine_instance_id or not job.machine_configuration_hash:
        raise HTTPException(status_code=409, detail="Bind a validated L32 machine instance before chain simulation")
    directory = job_directory(job_id)
    try:
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        if snapshot.configuration_hash != job.machine_configuration_hash:
            raise ValueError("bound machine configuration hash mismatch")
        rotational = RotationalFeatureAnalysis.model_validate_json(
            (directory / "rotational-features.json").read_text(encoding="utf-8")
        )
        profile = next((item for item in rotational.profiles if item.id == request.profile_id), None)
        if profile is None:
            raise ValueError("selected inner profile is not available")
        result = compile_inner_bore_chain(job_id, request, job.plan, profile, snapshot)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-inner-bore-chain-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-inner-bore-chain-draft.json", result.model_dump(mode="json"))
    return result


def _run_l32_operation_execution_agent(
    job_id: str,
    job: JobResponse,
    result: WholePartDraftResult,
) -> dict[str, object] | None:
    from .agent.config import load_agent_settings
    from .agent.l32_execution_graph import run_l32_operation_execution_graph
    from .agent.operation_review import review_operation_evidence
    from .agent.world_model import (
        apply_l32_draft_execution_trace,
        create_manufacturing_world_model,
        load_world_model,
    )
    from .qwen import load_qwen_settings

    settings = load_agent_settings()
    if not settings.enabled or not job.plan:
        return None
    directory = job_directory(job_id)
    operations = {
        operation.id: operation.model_dump(mode="json")
        for setup in job.plan.setups for operation in setup.operations
        if operation.enabled
    }
    ai_review_enabled = (
        settings.writes_production_results
        and isinstance(job.plan.ai_planning, dict)
        and job.plan.ai_planning.get("planner") == "langgraph_ai_primary"
    )
    qwen_settings = load_qwen_settings() if ai_review_enabled else None
    model_view = directory / "agent-perception-contact-sheet.png"

    def review_record(record: dict[str, object]) -> dict[str, object]:
        return review_operation_evidence(
            str(record["operation_id"]),
            {
                "machine": "Citizen Cincom L32",
                "release_status": "DRAFT",
                "machine_configuration_hash": result.machine_configuration_hash,
                "operation": record.get("operation"),
                "channel_id": record.get("channel_id"),
                "phase": record.get("phase"),
                "verification_status": record.get("verification_status"),
                "evidence": record.get("evidence"),
                "blocking_reasons": record.get("blocking_reasons"),
                "known_limitations": [
                    "轴对称 Z-R 连续材料仿真",
                    "尚未完成三维整机、刀杆、夹头和导套碰撞验证",
                    "尚未认证 MELDAS/CINCOM 后处理器",
                ],
            },
            model_view=model_view if model_view.is_file() else None,
            settings=qwen_settings,
        )

    def report(stage: str, message: str, **details: object) -> None:
        operation_id = details.get("operation_id")
        terminal = stage in {"review_operation", "summarize_execution"}
        publish_job_event(
            job_id, "l32_operation_execution", message, 99,
            agent_node=stage,
            agent_title=("L32 逐工序审核汇总" if stage == "summarize_execution" else f"L32 工序审核 · {operation_id or stage}"),
            agent_kind="validation" if terminal else "tool_result",
            agent_status="completed" if terminal else "running",
            viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"} if operation_id else None,
            evidence=[
                {"label": key, "value": value}
                for key, value in details.items()
                if isinstance(value, (str, int, float, bool))
            ],
            **details,
        )

    trace = run_l32_operation_execution_graph(
        job_id=job_id,
        stages=[item.model_dump(mode="json") for item in result.stages],
        operations=operations,
        toolpath=result.toolpath.model_dump(mode="json"),
        continuous_simulation=result.continuous_simulation.model_dump(mode="json"),
        settings=settings,
        progress_callback=report,
        review_callback=review_record if ai_review_enabled else None,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "mode": settings.mode,
        "review_mode": (
            "multimodal_ai" if ai_review_enabled and model_view.is_file()
            else "structured_ai" if ai_review_enabled else "deterministic_rules"
        ),
        "release_status": "DRAFT",
        "production_ready": False,
        "status": trace.get("status"),
        "summary": trace.get("summary", {}),
        "records": trace.get("records", []),
    }
    write_json(directory / "agent-l32-execution.json", payload)

    world_path = directory / "agent-world-model.json"
    world = load_world_model(world_path)
    if world is None and job.analysis:
        world = create_manufacturing_world_model(
            job_id=job_id, analysis=job.analysis, plan=job.plan,
            material=job.material, machine=job.machine, filename=job.filename,
            model_url=job.model_url,
        )
    if world is not None:
        world = apply_l32_draft_execution_trace(world, payload)
        write_json(world_path, world.model_dump(mode="json"))
    publish_job_event(
        job_id, "l32_operation_execution", "L32 逐工序 DRAFT 审核记录已归档", 100,
        agent_node="archive_l32_execution", agent_title="L32 执行证据",
        agent_kind="artifact", agent_status="completed",
        execution_status=payload["status"], release_status="DRAFT", production_ready=False,
        artifacts=[{
            "id": "agent-l32-execution", "label": "L32 逐工序 DRAFT 审核", "kind": "json",
            "url": f"/api/v1/jobs/{job_id}/files/agent-l32-execution.json",
        }],
    )
    return payload


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
        if job.analysis:
            job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
        uncovered_targets = [
            item for item in (job.plan.coverage.targets if job.plan.coverage else [])
            if item.state != "covered"
        ]
        if uncovered_targets:
            labels = ", ".join(item.label for item in uncovered_targets)
            raise ValueError(
                "whole-part L32 program is blocked because manufacturing targets are not covered: "
                + labels
            )
        source_axis = next((
            item for item in rotational.axes if item.id == source_profile.axis_id
        ), None)
        if source_axis is None or source_axis.review_state != "accepted":
            raise ValueError("source rotational axis must be accepted before whole-part planning")
        transverse_aspect_ratio = float(rotational.evidence.get("transverse_aspect_ratio", 1))
        if transverse_aspect_ratio < 0.9:
            raise ValueError(
                "whole-part L32 turning is blocked because the source solid is not fully rotational "
                f"(transverse aspect ratio {transverse_aspect_ratio:.3f})"
            )
        result = compile_whole_part_draft(
            job_id, request, job.plan, source_profile, snapshot,
        )
        compiled_operations = {stage.operation_id for stage in result.stages}
        independently_compiled_types = {
            "pocket_roughing", "pocket_finishing",
            "live_tool_contour_roughing", "live_tool_contour_finishing",
        }
        omitted_operations = [
            operation.id
            for setup in job.plan.setups for operation in setup.operations
            if operation.enabled
            and operation.type not in independently_compiled_types
            and operation.id not in compiled_operations
        ]
        if omitted_operations:
            raise ValueError(
                "whole-part L32 program omits enabled operations: "
                + ", ".join(omitted_operations)
            )
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    write_json(directory / "turning-whole-program-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-timeline.json", result.timeline.model_dump(mode="json"))
    write_json(directory / "turning-continuous-simulation.json", result.continuous_simulation.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-draft.json", result.model_dump(mode="json"))
    _run_l32_operation_execution_agent(job_id, job, result)
    return result


def _create_l32_whole_program_with_agent_loop(job_id: str) -> WholePartDraftResult:
    """Compile the L32 whole-part DRAFT and run its per-operation evidence graph.

    Planning used to skip L32 here because the generic FreeCAD CAM pipeline is
    not its execution engine.  The L32 compiler already owns a continuous stock
    simulation and an operation execution graph, so use that native path.
    """
    job = load_job(job_id)
    if not job.plan or not job.machine_instance_id:
        raise ValueError("L32 工艺尚未绑定可验证的机床实例")
    directory = job_directory(job_id)
    rotational = RotationalFeatureAnalysis.model_validate_json(
        (directory / "rotational-features.json").read_text(encoding="utf-8")
    )
    accepted_profile = next(
        (item for item in rotational.profiles if item.review_state == "accepted"), None,
    )
    # Whole-program validation may use a complete AI-provisional exact section,
    # but it must never silently promote an unreviewed or partial contour.
    source_profile = accepted_profile or max(
        (item for item in rotational.profiles if item.review_state == "ai_provisional"),
        key=lambda item: (item.confidence, len(item.points)),
        default=None,
    )
    if source_profile is None:
        raise ValueError("L32 试算缺少可用的回转轮廓")
    if (
        source_profile.review_state == "ai_provisional"
        and (
            source_profile.provisional_decision is None
            or source_profile.provisional_decision.scope != "full"
        )
    ):
        raise ValueError(
            "A partial AI-provisional profile is valid only for bounded per-operation DRAFT trials"
        )
    z_values = [point.z for point in source_profile.points]
    z_min, z_max = min(z_values), max(z_values)
    length = max(z_max - z_min, 1.0)
    cutoff = next((
        operation for setup in job.plan.setups for operation in setup.operations
        if operation.id == "OP40"
    ), None)
    cutoff_z = float(cutoff.parameters.get("z_mm", z_min)) if cutoff else z_min
    stock_radius = float(job.plan.stock.get("diameter_mm", 0) or 0) / 2
    if stock_radius <= 0:
        raise ValueError("L32 整件仿真缺少有效棒料直径")
    request = WholePartDraftRequest(
        machine_instance_id=job.machine_instance_id,
        source_profile_id=source_profile.id,
        stock_radius_mm=stock_radius,
        initial_bore_radius_mm=0,
        resolution_mm=0.1,
        approach_z_mm=z_max + 2,
        pickoff_z_mm=min(z_max - 0.2, cutoff_z + length * 0.6),
        grip_length_mm=min(max(length * 0.3, 0.5), 8),
        synchronization_rpm=1200,
        sub_spindle_clamp_confirmed=True,
    )
    snapshot = load_machine_snapshot(directory / "machine-configuration.json")
    if snapshot.configuration_hash != job.machine_configuration_hash:
        raise ValueError("bound machine configuration hash mismatch")
    compile_profile = (
        source_profile if source_profile.review_state == "accepted"
        else source_profile.model_copy(update={"review_state": "accepted"})
    )
    compile_plan = job.plan.model_copy(deep=True)
    if accepted_profile is None:
        compile_plan.stock["profile_review_state"] = "accepted"
    try:
        result = compile_whole_part_draft(job_id, request, compile_plan, compile_profile, snapshot)
    except ValueError as whole_error:
        # Preserve useful evidence instead of turning an all-program failure
        # into an empty validation screen.  Front operations are executed in
        # order against the previous stock state and the first real blocker is
        # retained for the repair/replan decision.
        from .turning_simulation import simulate_turning_stock

        operations = {
            operation.id: operation
            for setup in compile_plan.setups for operation in setup.operations
            if operation.enabled
        }
        trial_records: list[dict[str, object]] = []
        stock_samples = None
        trial_z_min, trial_z_max = z_min - 2, z_max + 2
        for operation_id in ("OP10", "OP20", "OP30"):
            operation = operations.get(operation_id)
            if operation is None:
                continue
            publish_job_event(
                job_id, "l32_operation_execution", f"正在试算 {operation_id} 并更新连续余料", 98,
                agent_node="execute_operation", agent_title=f"L32 工序试算 · {operation_id}",
                agent_kind="tool_call", agent_status="running", operation_id=operation_id,
                viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"},
            )
            try:
                draft = compile_turning_draft(
                    job_id,
                    TurningDraftRequest(
                        machine_instance_id=request.machine_instance_id,
                        operation=operation,
                        profile=compile_profile,
                        stock_radius_mm=request.stock_radius_mm,
                        initial_bore_radius_mm=request.initial_bore_radius_mm,
                        z_min_mm=trial_z_min,
                        z_max_mm=trial_z_max,
                        resolution_mm=request.resolution_mm,
                    ),
                    snapshot,
                )
                simulation = simulate_turning_stock(
                    draft.toolpath,
                    stock_radius_mm=request.stock_radius_mm,
                    initial_bore_radius_mm=request.initial_bore_radius_mm,
                    z_min_mm=trial_z_min,
                    z_max_mm=trial_z_max,
                    resolution_mm=request.resolution_mm,
                    initial_samples=stock_samples,
                )
                stock_samples = simulation.samples
                record = {
                    "operation_id": operation_id,
                    "status": "passed",
                    "command_count": sum(len(channel.commands) for channel in draft.toolpath.channels),
                    "removed_volume_mm3": simulation.metrics.removed_volume_mm3,
                    "remaining_volume_mm3": simulation.metrics.remaining_volume_mm3,
                }
                trial_records.append(record)
                write_json(directory / f"l32-trial-{operation_id}.json", {
                    "schema_version": "1.0.0", "release_status": "DRAFT",
                    "production_ready": False, "record": record,
                    "toolpath": draft.toolpath.model_dump(mode="json"),
                    "simulation": simulation.model_dump(mode="json"),
                })
                publish_job_event(
                    job_id, "l32_operation_execution", f"{operation_id} 试算完成，连续余料已更新", 98.5,
                    agent_node="review_operation", agent_title=f"L32 工序审核 · {operation_id}",
                    agent_kind="validation", agent_status="completed", operation_id=operation_id,
                    evidence=[
                        {"label": "去除体积", "value": round(simulation.metrics.removed_volume_mm3, 3)},
                        {"label": "剩余体积", "value": round(simulation.metrics.remaining_volume_mm3, 3)},
                    ],
                    viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"},
                )
            except (OSError, ValueError) as operation_error:
                trial_records.append({
                    "operation_id": operation_id, "status": "blocked", "reason": str(operation_error),
                })
                break
        cutoff_operation = operations.get("OP40")
        if cutoff_operation is not None:
            finished_back_datum_z = float(cutoff_operation.parameters.get(
                "finished_back_datum_z_mm", cutoff_operation.parameters.get("z_mm", z_min),
            ))
            backside_stock_radius = min(
                request.stock_radius_mm,
                max(point.radius for point in compile_profile.points) + 0.2,
            )
            for operation_id in ("OP50", "OP55-BACK", "OP58-BACK"):
                operation = operations.get(operation_id)
                if operation is None:
                    continue
                try:
                    backside = compile_backside_draft(
                        job_id,
                        BacksideDraftRequest(
                            machine_instance_id=request.machine_instance_id,
                            source_profile_id=compile_profile.id,
                            operation=operation,
                            source_cutoff_z_mm=finished_back_datum_z,
                            stock_radius_mm=backside_stock_radius,
                            resolution_mm=request.resolution_mm,
                        ),
                        compile_profile,
                        snapshot,
                    )
                    record = {
                        "operation_id": operation_id,
                        "status": "passed",
                        "scope": "isolated_backside_trial",
                        "command_count": sum(
                            len(channel.commands) for channel in backside.draft.toolpath.channels
                        ),
                        "removed_volume_mm3": backside.draft.simulation.metrics.removed_volume_mm3,
                    }
                    trial_records.append(record)
                    publish_job_event(
                        job_id, "l32_operation_execution", f"{operation_id} 背面独立试算通过", 98.6,
                        agent_node="review_operation", agent_title=f"L32 工序审核 · {operation_id}",
                        agent_kind="validation", agent_status="completed", operation_id=operation_id,
                        evidence=[{"label": "去除体积", "value": round(backside.draft.simulation.metrics.removed_volume_mm3, 3)}],
                        viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"},
                    )
                except (OSError, ValueError) as operation_error:
                    trial_records.append({
                        "operation_id": operation_id,
                        "status": "blocked",
                        "scope": "isolated_backside_trial",
                        "reason": str(operation_error),
                    })
                    publish_job_event(
                        job_id, "l32_operation_execution", f"{operation_id} 被真实可达性约束阻断", 98.7,
                        agent_node="verify_operation", agent_title=f"L32 工序阻断 · {operation_id}",
                        agent_kind="error", agent_status="blocked", operation_id=operation_id,
                        evidence=[{"label": "阻断原因", "value": str(operation_error)[:500]}],
                        viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"},
                    )

        from .agent.config import load_agent_settings
        from .agent.l32_repair_graph import run_l32_repair_graph

        failed_operations = [
            str(item["operation_id"]) for item in trial_records
            if item.get("status") == "blocked"
        ]

        def report_repair(stage: str, message: str, **details: object) -> None:
            terminal = stage == "repair_decision"
            publish_job_event(
                job_id, "l32_repair", message, 98.8,
                agent_node=stage,
                agent_title="L32 失败诊断与修正",
                agent_kind="decision" if terminal else "reasoning",
                agent_status="waiting" if terminal and details.get("decision") == "human_review" else "completed" if terminal else "running",
                evidence=[
                    {"label": key, "value": value}
                    for key, value in details.items()
                    if isinstance(value, (str, int, float, bool))
                ],
            )

        coverage = evaluate_plan_coverage(job.analysis, job.plan) if job.analysis else None
        validated_repairs: dict[str, tuple[ProcessPlan, WholePartDraftResult]] = {}

        def validate_repair_candidate(candidate: dict[str, object]) -> dict[str, object]:
            if candidate.get("id") != "replace_turning_tool_hand":
                return {"status": "failed", "reason": "unsupported_repair_candidate"}
            candidate_plan = compile_plan.model_copy(deep=True)
            candidate_operations = {
                operation.id: operation
                for setup in candidate_plan.setups for operation in setup.operations
            }
            substitutions: list[dict[str, str]] = []
            for operation_id in failed_operations:
                operation = candidate_operations.get(operation_id)
                if operation is None or operation.type not in {"turn_od_roughing", "turn_od_finishing"}:
                    return {"status": "failed", "reason": f"{operation_id} 不支持左右手刀具自动替换"}
                positive = operation.parameters.get("cut_direction", "negative_z") == "positive_z"
                tool_id = (
                    "TURN-OD-L-R" if positive and operation.type == "turn_od_roughing"
                    else "TURN-OD-L-MICRO-F" if positive
                    else "TURN-OD-R" if operation.type == "turn_od_roughing"
                    else "TURN-OD-MICRO-F"
                )
                previous = operation.tool.id
                operation.tool = get_tool(tool_id)
                substitutions.append({"operation_id": operation_id, "from": previous, "to": tool_id})
            try:
                candidate_result = compile_whole_part_draft(
                    job_id, request, candidate_plan, compile_profile, snapshot,
                )
            except (OSError, ValueError) as candidate_error:
                return {"status": "failed", "reason": str(candidate_error), "substitutions": substitutions}
            validated_repairs[str(candidate["id"])] = (candidate_plan, candidate_result)
            return {
                "status": "passed",
                "whole_program_status": candidate_result.continuous_simulation.status,
                "substitutions": substitutions,
            }

        repair_trace = run_l32_repair_graph(
            job_id=job_id,
            blocker=str(whole_error),
            failed_operations=failed_operations,
            profile_review_state=source_profile.review_state,
            coverage_status=coverage.status if coverage else "unknown",
            manufacturing_context={
                "nonrotational_turning_limit_z_mm": compile_plan.stock.get("nonrotational_turning_limit_z_mm"),
                "nonrotational_region_z_mm": compile_plan.stock.get("nonrotational_region_z_mm"),
                "machine_capabilities": list(snapshot.validation.capabilities),
                "machine_instance_id": snapshot.instance.id,
            },
            settings=load_agent_settings(),
            validate_candidate=validate_repair_candidate,
            progress_callback=report_repair,
        )
        selected_repair = (repair_trace.get("summary") or {}).get("selected_candidate")
        selected_repair_id = str(selected_repair.get("id")) if isinstance(selected_repair, dict) else ""
        incremental = {
            "schema_version": "1.0.0", "job_id": job_id, "release_status": "DRAFT",
            "production_ready": False, "status": "blocked",
            "records": trial_records, "whole_program_blocker": str(whole_error),
            "repair": repair_trace.get("summary", {}),
            "repair_candidates": repair_trace.get("candidates", []),
            "next_action": (repair_trace.get("summary") or {}).get("next_action", "human_review"),
        }
        write_json(directory / "agent-l32-incremental-trial.json", incremental)
        if selected_repair_id in validated_repairs:
            repaired_plan, result = validated_repairs[selected_repair_id]
            job.plan = repaired_plan
            job.plan.warnings.append("智能体已应用通过整件重编译与连续余料验证的刀具左右手修正。")
            save_job(directory, job)
        else:
            raise ValueError(
                f"{whole_error}; 已完成 {sum(item['status'] == 'passed' for item in trial_records)} "
                "道前序工序试算，阻断证据已归档"
            ) from whole_error
    if accepted_profile is None:
        result.warnings.append("本次智能体试算使用尚待人工确认的回转轮廓，不得用于生产放行。")
    if job.analysis:
        coverage = evaluate_plan_coverage(job.analysis, job.plan)
        if coverage.status != "complete":
            result.warnings.append(
                f"整件制造覆盖尚不完整（{coverage.covered_count}/{coverage.target_count}）；"
                "当前结果只验证已编译的 L32 车削子集。"
            )
    write_json(directory / "turning-whole-program-ir.json", result.toolpath.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-timeline.json", result.timeline.model_dump(mode="json"))
    write_json(directory / "turning-continuous-simulation.json", result.continuous_simulation.model_dump(mode="json"))
    write_json(directory / "turning-whole-program-draft.json", result.model_dump(mode="json"))
    _run_l32_operation_execution_agent(job_id, job, result)
    return result


@app.post("/api/v1/jobs/{job_id}/agent/l32/validate")
def validate_l32_agent_plan(job_id: str) -> dict[str, object]:
    """Run the real L32 compile/simulate/review loop and return compact evidence.

    This endpoint is intentionally DRAFT-only.  A blocked operation is a useful
    agent result, so validation failures are returned as evidence instead of
    being flattened into an HTTP error with no machine-readable trace.
    """
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="该验证工具仅适用于 L32 任务")
    if job.status != "completed" or not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="需要已完成的几何分析与工艺草案")
    with L32_AGENT_VALIDATION_LOCK:
        if job_id in L32_AGENT_VALIDATING_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在执行 L32 智能体验证")
        L32_AGENT_VALIDATING_JOBS.add(job_id)
    directory = job_directory(job_id)
    publish_job_event(
        job_id, "l32_agent_validation", "Harness 已请求真实 L32 编译与逐工序仿真", 98,
        agent_node="harness_validate", agent_title="L32 工具验证",
        agent_kind="tool_call", agent_status="running",
    )
    try:
        error = ""
        try:
            result = _create_l32_whole_program_with_agent_loop(job_id)
            simulation_status = result.continuous_simulation.status
        except (OSError, ValueError, HTTPException) as exc:
            error = str(exc.detail if isinstance(exc, HTTPException) else exc)
            simulation_status = "blocked"
        execution = _optional_job_json(directory, "agent-l32-execution.json") or {}
        incremental = _optional_job_json(directory, "agent-l32-incremental-trial.json") or {}
        records = execution.get("records") or incremental.get("records") or []
        status = str(execution.get("status") or incremental.get("status") or simulation_status)
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "job_id": job_id,
            "status": status,
            "release_status": "DRAFT",
            "production_ready": False,
            "simulation_status": simulation_status,
            "operation_count": len(records),
            "operations": [
                {
                    "operation_id": item.get("operation_id"),
                    "status": item.get("status"),
                    "blocking_reasons": item.get("blocking_reasons") or (
                        [item.get("reason")] if item.get("reason") else []
                    ),
                    "evidence": item.get("evidence") or {
                        key: item.get(key) for key in (
                            "command_count", "removed_volume_mm3", "remaining_volume_mm3",
                        ) if item.get(key) is not None
                    },
                }
                for item in records if isinstance(item, dict)
            ],
            "summary": execution.get("summary") or incremental.get("repair") or {},
            "next_action": (
                (execution.get("summary") or {}).get("next_action")
                or incremental.get("next_action")
                or ("human_review" if error else "machine_level_validation")
            ),
            "error": error or None,
            "artifacts": [
                name for name in (
                    "turning-whole-program-ir.json", "turning-continuous-simulation.json",
                    "agent-l32-execution.json", "agent-l32-incremental-trial.json",
                ) if (directory / name).is_file()
            ],
        }
        publish_job_event(
            job_id, "l32_agent_validation",
            "L32 智能体验证通过" if status == "passed" else "L32 智能体验证已保留阻断证据",
            100, agent_node="harness_validate", agent_title="L32 工具验证",
            agent_kind="validation", agent_status="completed" if status == "passed" else "blocked",
            evidence=[
                {"label": "状态", "value": status},
                {"label": "已审核工序", "value": len(records)},
            ],
        )
        return payload
    finally:
        with L32_AGENT_VALIDATION_LOCK:
            L32_AGENT_VALIDATING_JOBS.discard(job_id)


def _public_l32_rolling_loop(payload: dict[str, object]) -> dict[str, object]:
    """Hide the cached validation envelope while keeping cited evidence compact."""
    return {key: value for key, value in payload.items() if key != "validation_cache"}


def _l32_nonrotational_trial_evidence(
    job_id: str, operation: Operation, *, job: JobResponse | None = None,
) -> dict[str, object] | None:
    """Build deterministic OCC evidence for indexed L32 milling operations."""
    if operation.type not in {
        "pocket_roughing", "pocket_finishing",
        "live_tool_contour_roughing", "live_tool_contour_finishing",
    }:
        return None
    job = job or load_job(job_id)
    snapshot_path = job_directory(job_id) / "machine-configuration.json"
    if not snapshot_path.is_file():
        return {
            "machine_capability_status": "failed",
            "target_protection_status": "not_run",
            "material_removal_status": "not_run",
            "operation_coverage_status": "not_run",
            "detail": "Bound L32 machine configuration is unavailable.",
        }
    snapshot = load_machine_snapshot(snapshot_path)
    capabilities = set(snapshot.validation.capabilities)
    modules = set(snapshot.instance.installed_modules)

    if operation.type in {"pocket_roughing", "pocket_finishing"}:
        feature_id = next((item for item in operation.feature_ids if item.startswith("MF-")), None)
        if feature_id is None:
            return {
                "machine_capability_status": "failed", "target_protection_status": "not_run",
                "material_removal_status": "not_run", "operation_coverage_status": "not_run",
                "detail": "Pocket operation has no accepted prismatic feature binding.",
            }
        feature = next((item for item in job.analysis.prismatic_features if item.id == feature_id), None)
        if feature is None:
            return {
                "machine_capability_status": "failed", "target_protection_status": "not_run",
                "material_removal_status": "not_run", "operation_coverage_status": "not_run",
                "detail": "Pocket feature is absent from the accepted geometry analysis.",
            }
        diameter = float(operation.tool.diameter_mm)
        depth_step = float(operation.parameters.get("step_down_mm") or 0.3)
        stepover = operation.parameters.get("step_over_mm")
        if not isinstance(stepover, (int, float)) or isinstance(stepover, bool):
            percent = operation.parameters.get("step_over_percent")
            stepover = diameter * float(percent) / 100 if isinstance(percent, (int, float)) else min(0.35, diameter * 0.35)
        try:
            draft = build_indexed_back_pocket_draft(
                job.analysis, feature, tool_diameter_mm=diameter,
                depth_step_mm=depth_step, stepover_mm=float(stepover),
            )
        except ValueError as error:
            return {
                "machine_capability_status": "passed", "target_protection_status": "not_run",
                "material_removal_status": "failed", "operation_coverage_status": "failed",
                "detail": str(error), "feature_id": feature_id,
            }
        draft.tool_catalog_match = operation.tool.catalog_match
        draft.bound_machine_has_required_module = (
            "U151B" in modules
            and snapshot.instance.id == job.machine_instance_id
            and snapshot.configuration_hash == job.machine_configuration_hash
        )
        sweep = _run_l32_pocket_sweep(job_id, draft)
        capability_passed = (
            "back_live_tool_milling" in capabilities
            and "U151B" in modules
            and draft.bound_machine_has_required_module is True
        )
        protection_passed = sweep.summed_target_contact_mm3 <= 0.0001
        removal_passed = sweep.removed_pocket_region_mm3 > 0.0001
        finishing = operation.type == "pocket_finishing"
        coverage_passed = (
            protection_passed and removal_passed
            and (not finishing or sweep.remaining_pocket_region_mm3 <= 0.0001)
        )
        return {
            "executor": "exact_occ_indexed_pocket_sweep",
            "feature_id": feature_id,
            "machine_capability_status": "passed" if capability_passed else "failed",
            "target_protection_status": "passed" if protection_passed else "failed",
            "material_removal_status": "passed" if removal_passed else "failed",
            "operation_coverage_status": "passed" if coverage_passed else "failed",
            "removed_volume_mm3": sweep.removed_pocket_region_mm3,
            "remaining_feature_material_mm3": sweep.remaining_pocket_region_mm3,
            "target_contact_mm3": sweep.summed_target_contact_mm3,
            "bound_module": "U151B",
            "artifacts": {
                "draft": f"/api/v1/jobs/{job_id}/l32/catalog-back-pocket/{feature_id}/draft",
                "sweep": f"/api/v1/jobs/{job_id}/l32/catalog-back-pocket/{feature_id}/sweep-check",
            },
            "diagnostic_hints": ([
                "Finishing still leaves material in the exact pocket region; choose a smaller tool or a validated corner-cleanup process."
            ] if finishing and not coverage_passed else []),
        }

    geometry = get_l32_catalog_ear_geometry(job_id)
    faces = geometry["side_faces"]
    if len(faces) != 2 or {int(face["normal_y"]) for face in faces} != {-1, 1}:
        return {
            "machine_capability_status": "passed", "target_protection_status": "not_run",
            "material_removal_status": "failed", "operation_coverage_status": "failed",
            "detail": "Two opposing exact radial faces are required for exterior clearing.",
        }
    stock_radius = float(job.plan.stock["diameter_mm"]) / 2
    diameter = float(operation.tool.diameter_mm)
    radial_step = float(operation.parameters.get("step_down_mm") or min(0.4, diameter * 0.8))
    stepover = operation.parameters.get("step_over_mm")
    if not isinstance(stepover, (int, float)) or isinstance(stepover, bool):
        percent = operation.parameters.get("step_over_percent")
        stepover = diameter * float(percent) / 100 if isinstance(percent, (int, float)) else min(0.35, diameter * 0.7)
    raw_clearance = operation.parameters.get("silhouette_clearance_mm")
    silhouette_clearance = float(raw_clearance) if isinstance(raw_clearance, (int, float)) else 0.18
    try:
        drafts = [
            build_l32_exterior_clear_draft(
                face, stock_radius_mm=stock_radius,
                access_sign=1 if face["normal_y"] > 0 else -1,
                tool_diameter_mm=diameter, radial_step_mm=radial_step,
                stepover_mm=float(stepover), silhouette_clearance_mm=silhouette_clearance,
            ).model_dump(mode="json")
            for face in faces
        ]
    except (KeyError, TypeError, ValueError) as error:
        return {
            "machine_capability_status": "passed", "target_protection_status": "not_run",
            "material_removal_status": "failed", "operation_coverage_status": "failed",
            "detail": str(error),
        }
    module_bound = (
        "U30B" in modules
        and snapshot.instance.id == job.machine_instance_id
        and snapshot.configuration_hash == job.machine_configuration_hash
    )
    sweep = _check_l32_reference_sweep(job_id, drafts, module_bound)
    capability_passed = (
        "live_tool_milling" in capabilities
        and "U30B" in modules
        and sweep.get("bound_machine_has_required_module") is True
    )
    protection_passed = bool(sweep.get("target_gouge_check_passed")) and float(
        sweep.get("missing_target_volume_mm3") or 0
    ) <= 0.0001
    removed = float(sweep.get("removed_stock_volume_mm3") or 0)
    remaining = float(sweep.get("remaining_excess_region_mm3") or 0)
    initial_excess = float(sweep.get("initial_excess_region_mm3") or 0)
    removal_passed = removed > 0.0001 and remaining <= initial_excess + 0.0001
    finishing = operation.type == "live_tool_contour_finishing"
    tolerance = max(0.001, initial_excess * 0.005)
    coverage_passed = (
        protection_passed and removal_passed
        and (not finishing or remaining <= tolerance)
    )
    return {
        "executor": "exact_occ_nonrotational_exterior_sweep",
        "feature_ids": operation.feature_ids,
        "machine_capability_status": "passed" if capability_passed else "failed",
        "target_protection_status": "passed" if protection_passed else "failed",
        "material_removal_status": "passed" if removal_passed else "failed",
        "operation_coverage_status": "passed" if coverage_passed else "failed",
        "removed_volume_mm3": removed,
        "initial_excess_region_mm3": initial_excess,
        "remaining_excess_region_mm3": remaining,
        "coverage_tolerance_mm3": tolerance,
        "bound_module": "U30B",
        "artifacts": {
            "draft": f"/api/v1/jobs/{job_id}/l32/catalog-exterior-toolpaths",
            "sweep": f"/api/v1/jobs/{job_id}/l32/catalog-exterior-sweep-check",
        },
        "diagnostic_hints": ([
            "Exact exterior sweep leaves material in the nonrotational region; revise the live-tool strategy before finishing acceptance."
        ] if finishing and not coverage_passed else []),
    }


@app.get("/api/v1/jobs/{job_id}/agent/l32/operation-trials/state")
def get_l32_independent_operation_state(job_id: str) -> dict[str, object]:
    """Return the accepted operation prefix without exposing the full stock sample field."""
    from .agent.l32_operation_trial import (
        STATE_SCHEMA_VERSION, evidence_context_signature, reconcile_state, state_summary,
    )

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Independent operation trials currently require an L32 job")
    directory = job_directory(job_id)
    state = _optional_job_json(directory, "harness-l32-operation-state.json")
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    context_signature = None
    if rotational_path.is_file() and snapshot_path.is_file():
        context_signature = evidence_context_signature(
            job,
            RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8")),
            load_machine_snapshot(snapshot_path),
        )
    reconciled = reconcile_state(job, state, context_signature)
    if reconciled != state:
        write_json(directory / "harness-l32-operation-state.json", reconciled)
    summary = state_summary(job, reconciled)
    latest = _optional_job_json(directory, "agent-l32-operation-trial.json") or {}
    latest_public = {
        key: latest.get(key)
        for key in ("operation_id", "status", "can_accept", "reason", "detail", "next_action", "evidence")
        if latest.get(key) is not None
    }
    if latest_public and context_signature and latest.get("context_signature") != context_signature:
        latest_public.update({
            "status": "stale", "can_accept": False,
            "reason": "evidence_context_changed",
            "detail": "Geometry, stock or machine context changed; run the operation trial again.",
            "next_action": "trial_l32_operation",
        })
    elif latest_public and latest.get("schema_version") != STATE_SCHEMA_VERSION:
        latest_public.update({
            "status": "stale", "can_accept": False,
            "reason": "material_world_model_upgraded",
            "detail": "The material world model changed; retrial from the first operation on the fixed grid.",
            "next_action": "trial_l32_operation",
        })
    summary["latest_trial"] = latest_public or None
    return summary


@app.post("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/trial")
def trial_l32_operation_independently(job_id: str, operation_id: str) -> dict[str, object]:
    """Compile and simulate exactly the next operation from the accepted material state."""
    from .agent.l32_operation_trial import public_trial_result, run_independent_trial

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Independent operation trials currently require an L32 job")
    if job.status != "completed" or job.analysis is None or job.plan is None:
        raise HTTPException(status_code=409, detail="Completed geometry and a Harness process draft are required")
    directory = job_directory(job_id)
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    with L32_OPERATION_TRIAL_LOCK:
        if job_id in L32_OPERATION_TRIALING_JOBS:
            raise HTTPException(status_code=409, detail="An independent operation trial is already running")
        L32_OPERATION_TRIALING_JOBS.add(job_id)
    publish_job_event(
        job_id, "harness_operation_trial", f"正在独立编译并仿真 {operation_id}", 70,
        agent_node="harness_operation_trial", agent_title="单道工序试算",
        agent_kind="tool_call", agent_status="running", operation_id=operation_id,
    )
    try:
        rotational = RotationalFeatureAnalysis.model_validate_json(
            rotational_path.read_text(encoding="utf-8")
        )
        snapshot = load_machine_snapshot(snapshot_path)
        state = _optional_job_json(directory, "harness-l32-operation-state.json")
        try:
            operation = next(
                item for setup in job.plan.setups for item in setup.operations
                if item.enabled and item.id == operation_id
            )
            nonrotational_evidence = _l32_nonrotational_trial_evidence(job_id, operation)
            result, reconciled = run_independent_trial(
                job=job, operation_id=operation_id, rotational=rotational,
                snapshot=snapshot, state=state,
                nonrotational_evidence=nonrotational_evidence,
            )
        except (StopIteration, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        write_json(directory / "harness-l32-operation-state.json", reconciled)
        write_json(directory / "agent-l32-operation-trial.json", result)
        public = public_trial_result(result)
        public["artifacts"] = {
            "trial": f"/api/v1/jobs/{job_id}/files/agent-l32-operation-trial.json",
            "state": f"/api/v1/jobs/{job_id}/files/harness-l32-operation-state.json",
        }
        publish_job_event(
            job_id, "harness_operation_trial",
            f"{operation_id} 单道试算{'通过' if result.get('can_accept') else '被阻断'}", 82,
            agent_node="harness_operation_trial", agent_title="单道工序试算",
            agent_kind="validation",
            agent_status="completed" if result.get("can_accept") else "blocked",
            operation_id=operation_id,
            evidence=[
                {"label": "门禁状态", "value": result.get("status")},
                {"label": "材料去除 mm³", "value": (result.get("evidence") or {}).get("removed_volume_mm3")},
                {"label": "轮廓验证", "value": (result.get("evidence") or {}).get("verification_status")},
                {"label": "可达性", "value": (result.get("evidence") or {}).get("reachability_status")},
            ],
        )
        return public
    finally:
        with L32_OPERATION_TRIAL_LOCK:
            L32_OPERATION_TRIALING_JOBS.discard(job_id)


@app.post("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/trial/accept")
def accept_l32_independent_operation_trial(
    job_id: str, operation_id: str, request: L32OperationTrialDecisionRequest,
) -> dict[str, object]:
    """Accept planning evidence only; this never approves production or releases NC."""
    from .agent.l32_operation_trial import (
        accept_trial, evidence_context_signature, state_summary,
    )

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.plan is None:
        raise HTTPException(status_code=409, detail="An L32 process draft is required")
    directory = job_directory(job_id)
    state = _optional_job_json(directory, "harness-l32-operation-state.json")
    trial = _optional_job_json(directory, "agent-l32-operation-trial.json")
    if not trial:
        raise HTTPException(status_code=409, detail="No independent operation trial is available")
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    context_signature = evidence_context_signature(
        job,
        RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8")),
        load_machine_snapshot(snapshot_path),
    )
    try:
        accepted = accept_trial(
            job=job, state=state, trial=trial, operation_id=operation_id,
            context_signature=context_signature,
            decision_rationale=request.rationale,
            acknowledge_warning=request.acknowledge_warning,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    write_json(directory / "harness-l32-operation-state.json", accepted)
    summary = state_summary(job, accepted)
    summary.update({
        "decision": "accepted_for_next_planning_step",
        "operation_id": operation_id,
        "evidence": trial.get("evidence") or {},
        "next_action": (
            "add_next_operation" if summary.get("next_operation_id") is None
            else "trial_l32_operation"
        ),
        "notice": "Accepted only inside the DRAFT planning loop; no NC or production release was granted.",
        "decision_rationale": request.rationale,
        "warning_acknowledged": request.acknowledge_warning,
    })
    publish_job_event(
        job_id, "harness_operation_accept", f"{operation_id} 已通过规划门禁，余料状态已传给下一道", 86,
        agent_node="harness_operation_accept", agent_title="接受单道工序证据",
        agent_kind="decision", agent_status="completed", operation_id=operation_id,
        evidence=[
            {"label": "已接受工序", "value": summary["accepted_count"]},
            {"label": "下一工序", "value": summary.get("next_operation_id") or "等待规划"},
            {"label": "生产放行", "value": "否"},
            {"label": "决策理由", "value": request.rationale},
        ],
    )
    return summary


def _apply_l32_trial_candidate(
    job: JobResponse, operation_id: str, candidate: object,
) -> tuple[JobResponse, object]:
    candidate_job = job.model_copy(deep=True)
    target_operation_id = candidate.target_operation_id or operation_id
    operation = next((
        item for setup in candidate_job.plan.setups for item in setup.operations
        if item.id == target_operation_id
    ), None) if candidate_job.plan else None
    if operation is None:
        raise ValueError("operation does not exist in the current process draft")
    definition = get_operation_definition(operation.definition_id or operation.type)
    feature_ids = candidate.feature_ids if candidate.feature_ids is not None else operation.feature_ids
    if candidate.feature_ids is not None:
        _validate_operation_geometry(candidate_job, definition, feature_ids)
    operation.feature_ids = feature_ids
    if candidate.reference_profile_id is not None:
        _validate_reference_profile(candidate_job, candidate.reference_profile_id)
        operation.reference_profile_id = candidate.reference_profile_id
    if candidate.tool_id:
        tool = get_tool(candidate.tool_id)
        if tool.kind not in definition.tool.accepts:
            raise ValueError(f"tool type {tool.kind} is incompatible with {definition.name}")
        operation.tool = tool
    operation.parameters = validate_parameters(
        definition, {**operation.parameters, **candidate.parameters},
    )
    operation.rationale = [*operation.rationale, f"Harness repair candidate {candidate.id}: {candidate.rationale}"]
    operation.status = "proposed"
    operation.generation_state = "dirty"
    apply_cutting_parameters(
        operation, resolve_material(candidate_job.material), resolve_machine(candidate_job.machine),
    )
    return candidate_job, operation


def _l32_candidate_rank(trial: dict[str, object]) -> dict[str, object]:
    """Rank trial evidence while keeping deterministic acceptance as a hard gate."""
    evidence = trial.get("evidence") if isinstance(trial.get("evidence"), dict) else {}
    verification = (
        evidence.get("verification_metrics")
        if isinstance(evidence, dict) and isinstance(evidence.get("verification_metrics"), dict)
        else {}
    )
    overcut_count = int(verification.get("overcut_sample_count") or 0)
    excess_count = int(verification.get("excess_stock_sample_count") or 0)
    max_overcut = float(verification.get("maximum_overcut_mm") or 0)
    max_excess = float(verification.get("maximum_excess_stock_mm") or 0)
    excess_volume = float(verification.get("estimated_excess_stock_volume_mm3") or 0)
    command_count = int(evidence.get("command_count") or 0) if isinstance(evidence, dict) else 0
    remaining_feature = float(evidence.get("remaining_feature_material_mm3") or 0) if isinstance(evidence, dict) else 0
    remaining_exterior = float(evidence.get("remaining_excess_region_mm3") or 0) if isinstance(evidence, dict) else 0
    target_contact = float(evidence.get("target_contact_mm3") or 0) if isinstance(evidence, dict) else 0
    reachability = str(evidence.get("reachability_status") or "unknown") if isinstance(evidence, dict) else "unknown"
    can_accept = bool(trial.get("can_accept"))
    status = str(trial.get("status") or "blocked")

    # Unsafe or incomplete trials may still carry a diagnostic score so the
    # agent can explain measurable improvement, but they are never eligible.
    diagnostic_score = max(
        0.0,
        100.0
        - min(45.0, overcut_count * 3.0 + max_overcut * 20.0)
        - min(35.0, excess_count * 0.5 + max_excess * 5.0 + excess_volume * 0.02)
        - min(10.0, command_count * 0.02)
        - min(45.0, (remaining_feature + remaining_exterior) * 2.0 + target_contact * 100.0),
    )
    if reachability not in {"passed", "not_required"}:
        diagnostic_score = max(0.0, diagnostic_score - 20.0)
    eligible = can_accept and status in {"passed", "warning"}
    rank_score = (100.0 if status == "passed" else 75.0) - min(5.0, command_count * 0.005) if eligible else 0.0
    return {
        "rank_score": round(rank_score, 3),
        "diagnostic_score": round(diagnostic_score, 3),
        "eligible_for_application": eligible,
        "quality_metrics": {
            "overcut_sample_count": overcut_count,
            "excess_stock_sample_count": excess_count,
            "maximum_overcut_mm": max_overcut,
            "maximum_excess_stock_mm": max_excess,
            "estimated_excess_stock_volume_mm3": excess_volume,
            "command_count": command_count,
            "reachability_status": reachability,
            "remaining_feature_material_mm3": remaining_feature,
            "remaining_excess_region_mm3": remaining_exterior,
            "target_contact_mm3": target_contact,
        },
    }


def _l32_candidate_improvement(
    baseline: dict[str, object], candidate: dict[str, object],
) -> dict[str, object]:
    baseline_rank = _l32_candidate_rank(baseline)
    candidate_rank = _l32_candidate_rank(candidate)
    base_metrics = baseline_rank["quality_metrics"]
    candidate_metrics = candidate_rank["quality_metrics"]
    return {
        "diagnostic_score_delta": round(
            float(candidate_rank["diagnostic_score"]) - float(baseline_rank["diagnostic_score"]), 3,
        ),
        "excess_stock_sample_reduction": (
            int(base_metrics["excess_stock_sample_count"])
            - int(candidate_metrics["excess_stock_sample_count"])
        ),
        "excess_stock_volume_reduction_mm3": round(
            float(base_metrics["estimated_excess_stock_volume_mm3"])
            - float(candidate_metrics["estimated_excess_stock_volume_mm3"]), 6,
        ),
        "maximum_excess_reduction_mm": round(
            float(base_metrics["maximum_excess_stock_mm"])
            - float(candidate_metrics["maximum_excess_stock_mm"]), 6,
        ),
        "nonrotational_residual_reduction_mm3": round(
            float(base_metrics["remaining_feature_material_mm3"])
            + float(base_metrics["remaining_excess_region_mm3"])
            - float(candidate_metrics["remaining_feature_material_mm3"])
            - float(candidate_metrics["remaining_excess_region_mm3"]),
            6,
        ),
        "became_acceptable": (
            not bool(baseline_rank["eligible_for_application"])
            and bool(candidate_rank["eligible_for_application"])
        ),
    }


@app.post("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/candidates/evaluate")
def evaluate_l32_operation_candidates(
    job_id: str, operation_id: str, request: L32OperationCandidateEvaluationRequest,
) -> dict[str, object]:
    """Evaluate several AI-proposed repairs in isolation without mutating the formal plan."""
    from .agent.l32_operation_trial import (
        evidence_context_signature, operation_signature, public_trial_result,
        reconcile_state, run_independent_trial,
    )

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None or job.plan is None:
        raise HTTPException(status_code=409, detail="A completed L32 Harness process draft is required")
    candidate_ids = [candidate.id for candidate in request.candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise HTTPException(status_code=422, detail="Candidate ids must be unique")
    directory = job_directory(job_id)
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    rotational = RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8"))
    snapshot = load_machine_snapshot(snapshot_path)
    context_signature = evidence_context_signature(job, rotational, snapshot)
    state = reconcile_state(
        job, _optional_job_json(directory, "harness-l32-operation-state.json"), context_signature,
    )
    operations = [
        operation for setup in job.plan.setups for operation in sorted(setup.operations, key=lambda item: item.sequence)
        if operation.enabled
    ]
    accepted_count = len(state["accepted"])
    if accepted_count >= len(operations) or operations[accepted_count].id != operation_id:
        expected = operations[accepted_count].id if accepted_count < len(operations) else None
        raise HTTPException(status_code=409, detail=f"Candidate sandbox expects next operation {expected}")
    base_operation = operations[accepted_count]
    all_operations = {
        item.id: item for setup in job.plan.setups for item in setup.operations
    }
    base_target_signatures = {
        candidate.target_operation_id: operation_signature(all_operations[candidate.target_operation_id])
        for candidate in request.candidates
        if candidate.target_operation_id
        and candidate.target_operation_id in all_operations
        and candidate.target_operation_id != operation_id
    }
    results: list[dict[str, object]] = []
    full_results: list[dict[str, object]] = []
    for position, candidate in enumerate(request.candidates):
        try:
            candidate_job, candidate_operation = _apply_l32_trial_candidate(job, operation_id, candidate)
            nonrotational_evidence = _l32_nonrotational_trial_evidence(
                job_id, candidate_operation, job=candidate_job,
            )
            trial, _ = run_independent_trial(
                job=candidate_job, operation_id=operation_id, rotational=rotational,
                snapshot=snapshot, state=state,
                nonrotational_evidence=nonrotational_evidence,
            )
            public = public_trial_result(trial)
            ranking = _l32_candidate_rank(trial)
            if not candidate_operation.tool.catalog_match:
                sandbox_acceptable = bool(ranking["eligible_for_application"])
                ranking.update({
                    "rank_score": 0.0,
                    "eligible_for_application": False,
                    "application_blocker": "candidate_tool_is_not_bound_to_verified_catalog_inventory",
                    "sandbox_acceptable_before_inventory_binding": sandbox_acceptable,
                    "evidence_request": inventory_binding_evidence_request(
                        candidate_operation.tool,
                        _load_tool_inventory(job.machine_instance_id) if job.machine_instance_id else [],
                    ),
                })
            record = {
                "candidate_id": candidate.id,
                "rationale": candidate.rationale,
                "target_operation_id": candidate.target_operation_id or operation_id,
                **ranking,
                "candidate_operation": candidate_operation.model_dump(mode="json"),
                "trial": trial,
                "position": position,
            }
            full_results.append(record)
            results.append({
                "candidate_id": candidate.id, "rationale": candidate.rationale,
                "target_operation_id": candidate.target_operation_id or operation_id,
                **ranking, "tool_id": candidate_operation.tool.id,
                "parameters": candidate.parameters,
                "trial": public,
            })
        except (HTTPException, ValueError, OSError) as error:
            detail = str(error.detail if isinstance(error, HTTPException) else error)
            record = {
                "candidate_id": candidate.id, "rationale": candidate.rationale,
                "rank_score": -1, "error": detail, "position": position,
            }
            full_results.append(record)
            results.append(record)
    ranked = sorted(
        results, key=lambda item: (
            -float(item.get("rank_score", -1)),
            -float(item.get("diagnostic_score", -1)),
            int(item.get("position", 0)),
        ),
    )
    recommended = next((item for item in ranked if item.get("eligible_for_application") is True), None)
    inventory_binding_candidate = next((
        item for item in ranked
        if item.get("sandbox_acceptable_before_inventory_binding") is True
        and item.get("application_blocker")
        == "candidate_tool_is_not_bound_to_verified_catalog_inventory"
    ), None)
    payload = {
        "schema_version": "1.0.0", "job_id": job_id, "operation_id": operation_id,
        "base_operation_signature": operation_signature(base_operation),
        "base_target_signatures": base_target_signatures,
        "context_signature": context_signature,
        "status": "candidate_available" if recommended else "no_candidate_passed",
        "recommended_candidate_id": recommended.get("candidate_id") if recommended else None,
        "candidates": results,
        "next_action": (
            "apply_l32_operation_candidate" if recommended
            else "inspect_l32_candidate_tool_requirements" if inventory_binding_candidate
            else "observe_or_propose_new_candidates"
        ),
        "release_status": "DRAFT", "production_ready": False,
    }
    write_json(directory / "agent-l32-operation-candidates.json", {
        **payload, "candidates": full_results,
    })
    publish_job_event(
        job_id, "harness_candidate_sandbox",
        f"{operation_id} 已完成 {len(results)} 个修正候选的隔离试算", 84,
        agent_node="harness_candidate_sandbox", agent_title="工序修正候选沙盒",
        agent_kind="validation", agent_status="completed" if recommended else "blocked",
        operation_id=operation_id,
        evidence=[
            {"label": "候选数量", "value": len(results)},
            {"label": "推荐候选", "value": recommended.get("candidate_id") if recommended else "无"},
        ],
    )
    return payload


@app.get(
    "/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/candidates/{candidate_id}/tool-requirements"
)
def inspect_l32_candidate_tool_requirements(
    job_id: str, operation_id: str, candidate_id: str,
) -> dict[str, object]:
    """Describe compatible inventory or the exact physical evidence still required."""
    from .agent.l32_operation_trial import operation_signature

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.plan is None or not job.machine_instance_id:
        raise HTTPException(status_code=409, detail="A machine-bound L32 process draft is required")
    evaluation = _optional_job_json(
        job_directory(job_id), "agent-l32-operation-candidates.json",
    ) or {}
    selected = next((
        item for item in evaluation.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id") == candidate_id
    ), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Evaluated repair candidate was not found")
    if str(selected.get("target_operation_id") or operation_id) != operation_id:
        raise HTTPException(status_code=409, detail="Tool requirements require the active operation")
    current_operation = next((
        item for setup in job.plan.setups for item in setup.operations if item.id == operation_id
    ), None)
    if current_operation is None:
        raise HTTPException(status_code=404, detail="Operation no longer exists")
    if evaluation.get("base_operation_signature") != operation_signature(current_operation):
        raise HTTPException(status_code=409, detail="Operation changed after candidate evaluation")
    candidate_operation = type(current_operation).model_validate(selected.get("candidate_operation"))
    evidence_request = inventory_binding_evidence_request(
        candidate_operation.tool, _load_tool_inventory(job.machine_instance_id),
    )
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "operation_id": operation_id,
        "candidate_id": candidate_id,
        "sandbox_acceptable_before_inventory_binding": selected.get(
            "sandbox_acceptable_before_inventory_binding", False,
        ),
        **evidence_request,
        "release_status": "DRAFT",
        "production_ready": False,
    }


@app.post(
    "/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/candidates/{candidate_id}/bind-tool"
)
def bind_l32_candidate_inventory_tool(
    job_id: str, operation_id: str, candidate_id: str,
    request: L32CandidateToolBindingRequest,
) -> dict[str, object]:
    """Bind measured machine inventory to a diagnostic candidate, then rerun its sandbox trial."""
    from .agent.l32_operation_trial import (
        evidence_context_signature, operation_signature, public_trial_result,
        reconcile_state, run_independent_trial,
    )

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.plan is None or not job.machine_instance_id:
        raise HTTPException(status_code=409, detail="A machine-bound L32 process draft is required")
    directory = job_directory(job_id)
    evaluation_path = directory / "agent-l32-operation-candidates.json"
    evaluation = _optional_job_json(directory, evaluation_path.name) or {}
    selected = next((
        item for item in evaluation.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id") == candidate_id
    ), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Evaluated repair candidate was not found")
    if str(selected.get("target_operation_id") or operation_id) != operation_id:
        raise HTTPException(status_code=409, detail="Inventory binding currently requires the active operation")
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    rotational = RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8"))
    snapshot = load_machine_snapshot(snapshot_path)
    current_context = evidence_context_signature(job, rotational, snapshot)
    current_operation = next((
        item for setup in job.plan.setups for item in setup.operations if item.id == operation_id
    ), None)
    if current_operation is None:
        raise HTTPException(status_code=404, detail="Operation no longer exists")
    if evaluation.get("context_signature") != current_context:
        raise HTTPException(status_code=409, detail="Geometry, stock or machine context changed; reevaluate candidates")
    if evaluation.get("base_operation_signature") != operation_signature(current_operation):
        raise HTTPException(status_code=409, detail="Operation changed after candidate evaluation")

    inventory = next((
        item for item in _load_tool_inventory(job.machine_instance_id)
        if item.inventory_id == request.inventory_id
    ), None)
    if inventory is None:
        raise HTTPException(status_code=404, detail="Physical tool inventory record was not found")
    candidate_operation = type(current_operation).model_validate(selected.get("candidate_operation"))
    try:
        candidate_operation.tool = bind_verified_inventory_tool(inventory, candidate_operation.tool)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    candidate_operation.parameters.update({
        "tool_inventory_id": inventory.inventory_id,
        "tool_capability_verification_reference": inventory.capability_verification_reference,
    })
    candidate_operation.rationale.append(
        f"Bound verified physical tool {inventory.inventory_id}: {request.rationale}"
    )
    candidate_job = job.model_copy(deep=True)
    target = next(
        item for setup in candidate_job.plan.setups for item in setup.operations
        if item.id == operation_id
    )
    target_setup = next(setup for setup in candidate_job.plan.setups if target in setup.operations)
    target_setup.operations[target_setup.operations.index(target)] = candidate_operation
    state = reconcile_state(
        job, _optional_job_json(directory, "harness-l32-operation-state.json"), current_context,
    )
    try:
        trial, _ = run_independent_trial(
            job=candidate_job, operation_id=operation_id, rotational=rotational,
            snapshot=snapshot, state=state,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    ranking = _l32_candidate_rank(trial)
    selected.update({
        **ranking,
        "candidate_operation": candidate_operation.model_dump(mode="json"),
        "trial": trial,
        "inventory_binding": {
            "machine_instance_id": job.machine_instance_id,
            "inventory_id": inventory.inventory_id,
            "verification_state": inventory.verification_state,
            "verified_by": inventory.capability_verified_by,
            "verification_reference": inventory.capability_verification_reference,
        },
    })
    selected.pop("application_blocker", None)
    selected.pop("evidence_request", None)
    recommended = next((
        item for item in evaluation.get("candidates", [])
        if isinstance(item, dict) and item.get("eligible_for_application") is True
    ), None)
    evaluation.update({
        "status": (
            "candidate_available" if recommended
            else "tool_evidence_required" if inventory_binding_candidate
            else "no_candidate_passed"
        ),
        "recommended_candidate_id": recommended.get("candidate_id") if recommended else None,
        "next_action": (
            "apply_l32_operation_candidate" if recommended else "revise_inventory_or_strategy"
        ),
    })
    write_json(evaluation_path, evaluation)
    public = {
        "schema_version": "1.0.0", "job_id": job_id, "operation_id": operation_id,
        "candidate_id": candidate_id, **ranking,
        "tool": candidate_operation.tool.model_dump(mode="json"),
        "inventory_binding": selected["inventory_binding"],
        "trial": public_trial_result(trial),
        "next_action": evaluation["next_action"],
        "release_status": "DRAFT", "production_ready": False,
    }
    publish_job_event(
        job_id, "harness_candidate_tool_binding",
        f"{operation_id} 候选已绑定现场刀具 {inventory.inventory_id} 并重新试算", 86,
        agent_node="harness_candidate_tool_binding", agent_title="绑定现场刀具并复验",
        agent_kind="validation",
        agent_status="completed" if ranking["eligible_for_application"] else "blocked",
        operation_id=operation_id,
        evidence=[
            {"label": "现场刀具", "value": inventory.inventory_id},
            {"label": "验证状态", "value": inventory.verification_state},
            {"label": "试算", "value": trial.get("status")},
        ],
    )
    return public


@app.post("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/repairs/auto-evaluate")
def auto_evaluate_l32_operation_repairs(job_id: str, operation_id: str) -> dict[str, object]:
    """Diagnose the next operation and sandbox evidence-led repair candidates."""
    from .agent.l32_operation_trial import _profile_for_operation
    from .agent.l32_repair_candidates import propose_l32_operation_repairs

    baseline = trial_l32_operation_independently(job_id, operation_id)
    directory = job_directory(job_id)
    full_trial = _optional_job_json(directory, "agent-l32-operation-trial.json") or {}
    job = load_job(job_id)
    if job.plan is None:
        raise HTTPException(status_code=409, detail="An L32 process draft is required")
    operation = next((
        item for setup in job.plan.setups for item in setup.operations
        if item.id == operation_id and item.enabled
    ), None)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation does not exist in the active draft")
    rotational_path = directory / "rotational-features.json"
    if not rotational_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry evidence is missing")
    rotational = RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8"))
    proposal = propose_l32_operation_repairs(
        operation, full_trial, _profile_for_operation(operation, rotational),
        operations=[item for setup in job.plan.setups for item in setup.operations],
        rotational=rotational,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "operation_id": operation_id,
        "baseline_trial": baseline,
        "proposal": proposal,
        "release_status": "DRAFT",
        "production_ready": False,
    }
    candidates = proposal.get("candidates") or []
    if baseline.get("can_accept"):
        payload.update({
            "status": "baseline_acceptable",
            "next_action": "accept_l32_operation_trial",
        })
    elif candidates:
        request = L32OperationCandidateEvaluationRequest.model_validate({"candidates": candidates})
        evaluation = evaluate_l32_operation_candidates(job_id, operation_id, request)
        for candidate_result in evaluation.get("candidates", []):
            if not isinstance(candidate_result, dict) or not isinstance(candidate_result.get("trial"), dict):
                continue
            candidate_result["improvement_vs_baseline"] = _l32_candidate_improvement(
                full_trial, candidate_result["trial"],
            )
        recommended_candidate_id = evaluation.get("recommended_candidate_id")
        evidence_candidate = next((
            item for item in evaluation.get("candidates", [])
            if isinstance(item, dict)
            and item.get("sandbox_acceptable_before_inventory_binding") is True
            and isinstance(item.get("evidence_request"), dict)
        ), None)
        diagnostic_candidate = max(
            (
                item for item in evaluation.get("candidates", [])
                if isinstance(item, dict) and isinstance(item.get("diagnostic_score"), (int, float))
            ),
            key=lambda item: float(item.get("diagnostic_score") or 0),
            default=None,
        )
        unresolved_groove = (
            not recommended_candidate_id
            and operation.type == "turn_grooving"
            and diagnostic_candidate is not None
            and int((diagnostic_candidate.get("quality_metrics") or {}).get("excess_stock_sample_count") or 0) > 0
        )
        if unresolved_groove:
            requirements = proposal.setdefault("capability_requirements", [])
            requirements.append({
                "target_operation_id": operation.id,
                "feature_ids": list(operation.feature_ids),
                "required_strategy": "verified_contour_grooving_or_narrower_catalogued_insert",
                "catalog_match": False,
                "reason": "all_sandboxed_catalogue_grooving_envelopes_leave_residual_stock",
                "next_action": "define_verified_contour_grooving_or_catalog_narrower_tool",
            })
        unresolved_nonrotational = (
            not recommended_candidate_id
            and operation.type in {
                "pocket_roughing", "pocket_finishing",
                "live_tool_contour_roughing", "live_tool_contour_finishing",
            }
        )
        if unresolved_nonrotational:
            requirements = proposal.setdefault("capability_requirements", [])
            if not requirements:
                requirements.append({
                    "target_operation_id": operation.id,
                    "feature_ids": list(operation.feature_ids),
                    "required_strategy": (
                        "sharp_corner_cleanup_or_accepted_internal_corner_radius"
                        if operation.type.startswith("pocket_")
                        else "multi_orientation_exterior_cleanup_or_smaller_verified_tool"
                    ),
                    "catalog_match": False,
                    "reason": "all_exact_occ_sandbox_candidates_leave_residual_or_violate_target_protection",
                    "next_action": "define_and_bind_verified_special_process",
                })
        payload.update({
            "status": (
                "tool_evidence_required" if evidence_candidate
                else "capability_required"
                if not recommended_candidate_id and proposal.get("capability_requirements")
                else evaluation.get("status")
            ),
            "evaluation": evaluation,
            "recommended_candidate_id": recommended_candidate_id,
            "best_diagnostic_candidate_id": (
                diagnostic_candidate.get("candidate_id") if diagnostic_candidate else None
            ),
            "tool_evidence_candidate_id": (
                evidence_candidate.get("candidate_id") if evidence_candidate else None
            ),
            "evidence_request": (
                evidence_candidate.get("evidence_request") if evidence_candidate else None
            ),
            "next_action": (
                evaluation.get("next_action") if recommended_candidate_id
                else "inspect_l32_candidate_tool_requirements" if evidence_candidate
                else "define_verified_contour_grooving_or_catalog_narrower_tool"
                if unresolved_groove
                else "define_and_bind_verified_special_process"
                if unresolved_nonrotational
                else proposal.get("fallback_next_action")
            ),
        })
    else:
        payload.update({
            "status": "observation_required",
            "recommended_candidate_id": None,
            "next_action": proposal.get("next_action"),
        })
    write_json(directory / "agent-l32-auto-repair.json", payload)
    publish_job_event(
        job_id, "harness_auto_repair",
        f"{operation_id} 自动诊断并生成 {len(candidates)} 个安全修复候选", 85,
        agent_node="harness_auto_repair", agent_title="工序失败自动诊断",
        agent_kind="reasoning",
        agent_status="completed" if payload.get("recommended_candidate_id") else "blocked",
        operation_id=operation_id,
        evidence=[
            {"label": "候选数量", "value": len(candidates)},
            {"label": "推荐候选", "value": payload.get("recommended_candidate_id") or "无"},
            {"label": "下一动作", "value": payload.get("next_action")},
        ],
    )
    return payload


@app.post("/api/v1/jobs/{job_id}/agent/l32/operations/{operation_id}/candidates/apply")
def apply_l32_operation_candidate(
    job_id: str, operation_id: str, request: L32OperationCandidateSelectionRequest,
) -> dict[str, object]:
    """Apply one already-simulated candidate after explicit model confirmation."""
    from .agent.l32_operation_trial import evidence_context_signature, operation_signature

    if not request.confirmed:
        raise HTTPException(status_code=422, detail="Candidate application requires confirmed=true")
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None or job.plan is None:
        raise HTTPException(status_code=409, detail="A completed L32 Harness process draft is required")
    directory = job_directory(job_id)
    evaluation = _optional_job_json(directory, "agent-l32-operation-candidates.json") or {}
    selected = next((
        item for item in evaluation.get("candidates", [])
        if isinstance(item, dict) and item.get("candidate_id") == request.candidate_id
    ), None)
    if selected is None:
        raise HTTPException(status_code=404, detail="Evaluated repair candidate was not found")
    if selected.get("eligible_for_application") is False:
        raise HTTPException(
            status_code=409,
            detail=str(selected.get("application_blocker") or "Candidate is diagnostic-only"),
        )
    trial = selected.get("trial") or {}
    if not trial.get("can_accept"):
        raise HTTPException(status_code=409, detail="Candidate did not pass the deterministic trial gate")
    if trial.get("status") == "warning" and not request.acknowledge_warning:
        raise HTTPException(status_code=422, detail="Warning candidate requires explicit acknowledgement")
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    current_context = evidence_context_signature(
        job,
        RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8")),
        load_machine_snapshot(snapshot_path),
    )
    operation = next((
        item for setup in job.plan.setups for item in setup.operations if item.id == operation_id
    ), None)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation was removed after candidate evaluation")
    if evaluation.get("context_signature") != current_context:
        raise HTTPException(status_code=409, detail="Geometry, stock or machine context changed; reevaluate candidates")
    if evaluation.get("base_operation_signature") != operation_signature(operation):
        raise HTTPException(status_code=409, detail="Operation changed after candidate evaluation")
    target_operation_id = str(selected.get("target_operation_id") or operation_id)
    target_operation = next((
        item for setup in job.plan.setups for item in setup.operations if item.id == target_operation_id
    ), None)
    if target_operation is None:
        raise HTTPException(status_code=404, detail="Candidate target operation no longer exists")
    if target_operation_id != operation_id and (
        (evaluation.get("base_target_signatures") or {}).get(target_operation_id)
        != operation_signature(target_operation)
    ):
        raise HTTPException(status_code=409, detail="Candidate target operation changed after evaluation")
    candidate_operation = type(target_operation).model_validate(selected.get("candidate_operation"))
    if not candidate_operation.tool.catalog_match:
        raise HTTPException(
            status_code=409,
            detail="Candidate tool must be bound to verified catalog inventory before application",
        )
    target_setup = next(setup for setup in job.plan.setups if target_operation in setup.operations)
    target_setup.operations[target_setup.operations.index(target_operation)] = candidate_operation
    saved = _save_manual_plan_change(job_id, job)
    selection = {
        "schema_version": "1.0.0", "job_id": job_id, "operation_id": operation_id,
        "target_operation_id": target_operation_id,
        "candidate_id": request.candidate_id, "status": "applied_for_retrial",
        "rationale": request.rationale, "warning_acknowledged": request.acknowledge_warning,
        "selected_trial_status": trial.get("status"),
        "new_operation_signature": operation_signature(candidate_operation),
        "next_action": "trial_l32_operation",
        "release_status": "DRAFT", "production_ready": False,
    }
    write_json(directory / "agent-l32-operation-candidate-selection.json", selection)
    publish_job_event(
        job_id, "harness_candidate_apply",
        f"{operation_id} 已应用修正候选 {request.candidate_id}，等待正式重试", 85,
        agent_node="harness_candidate_apply", agent_title="应用工序修正候选",
        agent_kind="decision", agent_status="completed", operation_id=operation_id,
        evidence=[
            {"label": "候选", "value": request.candidate_id},
            {"label": "决策理由", "value": request.rationale},
        ],
    )
    return {**selection, "operation": next(
        item.model_dump(mode="json") for setup in saved.plan.setups for item in setup.operations
        if item.id == target_operation_id
    )}


@app.post("/api/v1/jobs/{job_id}/agent/l32/plan/finalize")
def finalize_harness_l32_process_plan(job_id: str) -> dict[str, object]:
    """Close the Harness loop only after every operation and coverage target has evidence."""
    from .agent.l32_operation_trial import (
        enabled_operations, evidence_context_signature, reconcile_state, state_summary,
    )

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None or job.plan is None:
        raise HTTPException(status_code=409, detail="A completed L32 Harness process draft is required")
    directory = job_directory(job_id)
    rotational_path = directory / "rotational-features.json"
    snapshot_path = directory / "machine-configuration.json"
    if not rotational_path.is_file() or not snapshot_path.is_file():
        raise HTTPException(status_code=409, detail="Rotational geometry or machine configuration evidence is missing")
    context_signature = evidence_context_signature(
        job,
        RotationalFeatureAnalysis.model_validate_json(rotational_path.read_text(encoding="utf-8")),
        load_machine_snapshot(snapshot_path),
    )
    state = reconcile_state(
        job, _optional_job_json(directory, "harness-l32-operation-state.json"), context_signature,
    )
    write_json(directory / "harness-l32-operation-state.json", state)
    progress = state_summary(job, state)
    operation_count = len(enabled_operations(job))
    if operation_count == 0 or progress["accepted_count"] != operation_count:
        return {
            **progress,
            "status": "blocked",
            "reason": "unvalidated_operations",
            "detail": "Every enabled operation must pass and be accepted before final validation.",
            "next_action": "trial_l32_operation",
        }
    if (
        progress.get("provisional_operation_ids")
        or progress.get("unassigned_material_obligations")
        or progress.get("outstanding_deferred_material_contracts")
    ):
        return {
            **progress,
            "status": "blocked",
            "reason": "unresolved_material_obligations",
            "detail": (
                "Residual material responsibilities must be assigned, retrialed, and satisfied "
                "before final plan validation."
            ),
            "next_action": "reset_and_retrial_provisional_operations",
        }

    # Only accepted deterministic evidence may promote semantic coverage.
    job.plan.stock["verified_prismatic_feature_ids"] = progress.get(
        "verified_prismatic_feature_ids", []
    )
    job.plan.stock["nonrotational_material_verified"] = bool(
        progress.get("nonrotational_material_verified")
    )
    coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.coverage = coverage
    save_job(directory, job)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    if coverage.status != "complete":
        uncovered = [
            {
                "id": target.id, "kind": target.kind, "label": target.label,
                "state": target.state, "required_operation_types": target.required_operation_types,
                "source_feature_ids": target.source_feature_ids,
            }
            for target in coverage.targets if target.state != "covered"
        ]
        result = {
            **progress,
            "status": "plan_incomplete",
            "reason": "manufacturing_coverage_incomplete",
            "coverage": {
                "status": coverage.status, "score": coverage.score,
                "covered_count": coverage.covered_count, "target_count": coverage.target_count,
                "issues": coverage.issues, "capability_gaps": coverage.capability_gaps,
                "uncovered_targets": uncovered[:50],
            },
            "next_action": "inspect_geometry_then_add_process_operation",
        }
        publish_job_event(
            job_id, "harness_plan_finalize", "逐道试算已完成，但制造特征覆盖仍不完整", 90,
            agent_node="harness_plan_finalize", agent_title="工艺方案收口门禁",
            agent_kind="validation", agent_status="blocked",
            evidence=[
                {"label": "覆盖率", "value": coverage.score},
                {"label": "未覆盖目标", "value": len(uncovered)},
            ],
        )
        return result

    validation = validate_l32_agent_plan(job_id)
    passed = validation.get("status") == "passed"
    result = {
        **progress,
        "status": "validated_draft" if passed else "blocked",
        "reason": None if passed else "whole_plan_validation_failed",
        "coverage": {
            "status": coverage.status, "score": coverage.score,
            "covered_count": coverage.covered_count, "target_count": coverage.target_count,
        },
        "whole_plan_validation": validation,
        "next_action": "engineer_review" if passed else validation.get("next_action"),
        "release_status": "DRAFT",
        "production_ready": False,
    }
    write_json(directory / "harness-l32-plan-finalization.json", result)
    return result


def _run_l32_autonomous_process(
    job_id: str, request: L32AutonomousProcessRequest,
) -> dict[str, object]:
    """Run the bounded plan -> trial -> repair -> accept -> finalize loop."""
    from .agent.l32_autonomous_process import (
        budget_exhausted, classify_blocker, finish_state, new_state,
    )

    started = time.monotonic()
    limits = request.model_dump(mode="json")
    state = new_state(job_id, limits)
    directory = job_directory(job_id)
    state_path = directory / "agent-l32-autonomous-process.json"

    def persist() -> None:
        state["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(state_path, state)

    def use_tool(count: int = 1) -> None:
        state["usage"]["tool_calls"] += count
        persist()

    def stop_for_budget() -> dict[str, object] | None:
        reason = budget_exhausted(state, time.monotonic() - started)
        if not reason:
            return None
        finish_state(
            state, "engineer_review_required", next_action="resume_with_new_budget",
            blocker={"reason": reason, "detail": "Autonomous execution stopped at its configured safety budget."},
        )
        persist()
        return state

    persist()
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        finish_state(
            state, "capability_unavailable", next_action="select_supported_machine",
            blocker={"reason": "unsupported_machine", "detail": "The autonomous executor currently supports Citizen L32 only."},
        )
        persist()
        return state
    if job.status != "completed" or job.analysis is None:
        finish_state(
            state, "engineer_review_required", next_action="complete_geometry_analysis",
            blocker={"reason": "geometry_not_ready", "detail": "STEP geometry analysis is not complete."},
        )
        persist()
        return state

    enabled = [
        operation for setup in (job.plan.setups if job.plan else [])
        for operation in setup.operations if operation.enabled
    ]
    if request.rebuild_plan or job.plan is None or not enabled:
        state["phase"] = "hierarchical_process_planning"
        persist()
        plan = build_process_plan(job.analysis, material=job.material, machine=job.machine)
        plan.ai_planning = {
            "planning_owner": "bounded_autonomous_agent",
            "planning_mode": "hierarchical_plan_then_operation_evidence_loop",
            "release_status": "DRAFT",
            "model_calls": 0,
            "note": "Deterministic geometry and strategy baseline; every enabled operation still requires trial evidence.",
        }
        job.plan = plan
        for filename in (
            "harness-l32-operation-state.json", "agent-l32-operation-trial.json",
            "agent-l32-operation-candidates.json", "agent-l32-auto-repair.json",
            "harness-l32-plan-finalization.json",
        ):
            (directory / filename).unlink(missing_ok=True)
        provision_default_l32_planning_instance(job, directory)
        reapply_bound_l32_machine_configuration(job, directory)
        save_job(directory, job)
        write_json(directory / "plan.json", plan.model_dump(mode="json"))
        use_tool()

    job = load_job(job_id)
    assert job.plan is not None
    stock_diameter = float(job.plan.stock.get("diameter_mm") or 0)
    snapshot_path = directory / "machine-configuration.json"
    state["manufacturability"] = {
        "machine": "citizen-cincom-l32",
        "stock_type": job.plan.stock.get("type"),
        "stock_diameter_mm": stock_diameter,
        "machine_configuration_bound": snapshot_path.is_file(),
        "assessment": "supported_scope" if snapshot_path.is_file() and 0 < stock_diameter <= 38 else "outside_supported_scope",
    }
    if not snapshot_path.is_file() or stock_diameter <= 0 or stock_diameter > 38:
        finish_state(
            state, "capability_unavailable", next_action="bind_compatible_machine_or_change_stock_strategy",
            blocker={
                "reason": "l32_stock_or_machine_incompatible",
                "detail": f"L32 requires a bound configuration and round stock within 38 mm; draft diameter is {stock_diameter:g} mm.",
            },
        )
        persist()
        return state

    operations = [
        operation for setup in job.plan.setups
        for operation in sorted(setup.operations, key=lambda item: item.sequence)
        if operation.enabled
    ]
    state["manufacturability"]["enabled_operation_count"] = len(operations)
    if not operations:
        finish_state(
            state, "engineer_review_required", next_action="inspect_geometry_and_create_process_strategy",
            blocker={"reason": "empty_process_plan", "detail": "No enabled operation could be derived from the current geometry."},
        )
        persist()
        return state

    accepted_state = _optional_job_json(directory, "harness-l32-operation-state.json") or {}
    accepted_ids = [
        str(item.get("operation_id")) for item in accepted_state.get("accepted", [])
        if isinstance(item, dict) and item.get("operation_id")
    ]
    operation_index = 0
    for operation in operations:
        if operation_index < len(accepted_ids) and operation.id == accepted_ids[operation_index]:
            state["operations"].append({
                "operation_id": operation.id, "name": operation.name, "type": operation.type,
                "trial_status": "accepted_previous_run", "can_accept": True,
                "decision": "resumed_from_persisted_evidence",
            })
            operation_index += 1
        else:
            break
    state["resumed_accepted_operation_count"] = operation_index
    persist()
    repair_attempts: dict[str, int] = {}
    while operation_index < len(operations):
        if stop_for_budget():
            return state
        job = load_job(job_id)
        assert job.plan is not None
        operation = next((
            item for setup in job.plan.setups for item in setup.operations
            if item.id == operations[operation_index].id and item.enabled
        ), None)
        if operation is None:
            operations = [
                item for setup in job.plan.setups
                for item in sorted(setup.operations, key=lambda value: value.sequence) if item.enabled
            ]
            continue
        state["phase"] = "operation_trial"
        state["current_operation_id"] = operation.id
        publish_job_event(
            job_id, "autonomous_operation", f"自主智能体正在试算 {operation.id} {operation.name}",
            45 + 40 * operation_index / max(len(operations), 1),
            agent_node="autonomous_operation_trial", agent_title="逐工序规划、仿真与审核",
            agent_kind="tool_call", agent_status="running", operation_id=operation.id,
        )
        try:
            trial = trial_l32_operation_independently(job_id, operation.id)
        except HTTPException as error:
            finish_state(
                state, "engineer_review_required", next_action="inspect_operation_blocker",
                blocker={"operation_id": operation.id, "reason": "trial_api_error", "detail": str(error.detail)},
            )
            persist()
            return state
        use_tool()
        state["usage"]["operation_trials"] += 1
        record = {
            "operation_id": operation.id, "name": operation.name, "type": operation.type,
            "trial_status": trial.get("status"), "can_accept": trial.get("can_accept", False),
            "reason": trial.get("reason"), "repair_attempts": repair_attempts.get(operation.id, 0),
            "evidence": trial.get("evidence") or {},
        }
        state["operations"] = [
            item for item in state["operations"] if item.get("operation_id") != operation.id
        ] + [record]
        persist()

        if trial.get("can_accept"):
            accept_l32_independent_operation_trial(
                job_id, operation.id,
                L32OperationTrialDecisionRequest(
                    rationale="Autonomous deterministic gate passed; preserve evidence for the next operation.",
                    acknowledge_warning=bool(trial.get("requires_warning_acknowledgement")),
                ),
            )
            use_tool()
            record["decision"] = "accepted_for_next_planning_step"
            operation_index += 1
            continue

        attempts = repair_attempts.get(operation.id, 0)
        if attempts >= request.max_repair_attempts_per_operation:
            finish_state(
                state, "engineer_review_required", next_action="review_failed_operation",
                blocker={
                    "operation_id": operation.id, "reason": "repair_budget_exhausted",
                    "detail": str(trial.get("detail") or trial.get("reason") or "Operation trial failed."),
                },
            )
            persist()
            return state
        state["phase"] = "operation_repair"
        repair_attempts[operation.id] = attempts + 1
        state["usage"]["repair_attempts"] += 1
        repair = auto_evaluate_l32_operation_repairs(job_id, operation.id)
        candidate_count = len(((repair.get("proposal") or {}).get("candidates") or []))
        use_tool(max(1, candidate_count + 1))
        record["repair_attempts"] = repair_attempts[operation.id]
        record["repair_status"] = repair.get("status")
        record["recommended_candidate_id"] = repair.get("recommended_candidate_id")
        requirements = (repair.get("proposal") or {}).get("capability_requirements") or []
        for requirement in requirements:
            if requirement not in state["capability_requirements"]:
                state["capability_requirements"].append(requirement)
        candidate_id = repair.get("recommended_candidate_id")
        if candidate_id:
            recommended = next((
                item for item in ((repair.get("evaluation") or {}).get("candidates") or [])
                if isinstance(item, dict) and item.get("candidate_id") == candidate_id
            ), {})
            acknowledge_warning = bool(
                (recommended.get("trial") or {}).get("requires_warning_acknowledgement")
            )
            apply_l32_operation_candidate(
                job_id, operation.id,
                L32OperationCandidateSelectionRequest(
                    candidate_id=str(candidate_id),
                    rationale="Autonomous repair candidate passed the isolated deterministic trial.",
                    acknowledge_warning=acknowledge_warning, confirmed=True,
                ),
            )
            use_tool()
            record["repair_decision"] = "candidate_applied_for_formal_retrial"
            persist()
            continue

        outcome = classify_blocker({
            **repair,
            "capability_requirements": requirements,
            "reason": repair.get("status") or trial.get("reason"),
        })
        finish_state(
            state, outcome,
            next_action=str(repair.get("next_action") or "engineer_review"),
            blocker={
                "operation_id": operation.id,
                "reason": repair.get("status") or trial.get("reason"),
                "detail": str(trial.get("detail") or "No verified repair candidate passed."),
            },
        )
        persist()
        return state

    state["phase"] = "whole_plan_validation"
    finalization = finalize_harness_l32_process_plan(job_id)
    use_tool()
    state["coverage"] = finalization.get("coverage")
    state["whole_plan_validation"] = finalization.get("whole_plan_validation")
    if finalization.get("status") == "validated_draft":
        finish_state(state, "verified_success", next_action="engineer_review_and_machine_level_validation")
    else:
        requirements = ((finalization.get("coverage") or {}).get("capability_gaps") or [])
        state["capability_requirements"].extend(
            item for item in requirements if item not in state["capability_requirements"]
        )
        outcome = "capability_unavailable" if requirements else "engineer_review_required"
        finish_state(
            state, outcome, next_action=str(finalization.get("next_action") or "engineer_review"),
            blocker={
                "reason": finalization.get("reason") or "whole_plan_validation_failed",
                "detail": str(finalization.get("detail") or "The complete draft did not pass final validation."),
            },
        )
    persist()
    publish_job_event(
        job_id, "autonomous_process_finished",
        f"自主工艺执行结束：{state['outcome']}", 100,
        agent_node="autonomous_process_result", agent_title="工艺规划最终结论",
        agent_kind="decision", agent_status="completed",
        evidence=[
            {"label": "结论", "value": state["outcome"]},
            {"label": "已试算工序", "value": state["usage"]["operation_trials"]},
            {"label": "修复次数", "value": state["usage"]["repair_attempts"]},
        ],
    )
    return state


def _launch_l32_autonomous_process(
    job_id: str, request: L32AutonomousProcessRequest,
) -> dict[str, object]:
    from .agent.l32_autonomous_process import new_state

    with L32_AUTONOMOUS_PROCESS_LOCK:
        if job_id in L32_AUTONOMOUS_PROCESS_JOBS:
            raise HTTPException(status_code=409, detail="Autonomous L32 planning is already running")
        L32_AUTONOMOUS_PROCESS_JOBS.add(job_id)
    directory = job_directory(job_id)
    state = new_state(job_id, request.model_dump(mode="json"))
    write_json(directory / "agent-l32-autonomous-process.json", state)

    def worker() -> None:
        try:
            _run_l32_autonomous_process(job_id, request)
        except Exception as error:
            failed = _optional_job_json(directory, "agent-l32-autonomous-process.json") or state
            failed.update({
                "status": "completed", "outcome": "engineer_review_required",
                "phase": "finished", "finished_at": utc_now(),
                "next_action": "inspect_executor_error",
            })
            failed.setdefault("blockers", []).append({
                "reason": "executor_error", "detail": str(error)[-2000:],
            })
            write_json(directory / "agent-l32-autonomous-process.json", failed)
        finally:
            with L32_AUTONOMOUS_PROCESS_LOCK:
                L32_AUTONOMOUS_PROCESS_JOBS.discard(job_id)

    threading.Thread(
        target=worker, name=f"l32-autonomous-{job_id[:8]}", daemon=True,
    ).start()
    return state


@app.post("/api/v1/jobs/{job_id}/agent/l32/autonomous/start")
def start_l32_autonomous_process(
    job_id: str, request: L32AutonomousProcessRequest,
) -> dict[str, object]:
    load_job(job_id)
    return _launch_l32_autonomous_process(job_id, request)


@app.get("/api/v1/jobs/{job_id}/agent/l32/autonomous")
def get_l32_autonomous_process(job_id: str) -> dict[str, object]:
    load_job(job_id)
    payload = _optional_job_json(job_directory(job_id), "agent-l32-autonomous-process.json")
    if payload is None:
        return {
            "schema_version": "1.0.0", "job_id": job_id, "status": "not_started",
            "outcome": None, "next_action": "start_autonomous_process",
            "release_status": "DRAFT", "production_ready": False,
        }
    return payload


@app.get("/api/v1/jobs/{job_id}/agent/l32/loop")
def get_l32_agent_rolling_loop(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="滚动工序闭环目前仅适用于 L32 任务")
    payload = _optional_job_json(job_directory(job_id), "agent-l32-rolling-loop.json")
    if not payload:
        return {
            "schema_version": "1.0.0", "job_id": job_id, "status": "not_started",
            "release_status": "DRAFT", "production_ready": False,
            "accepted_operation_ids": [], "accepted_count": 0,
            "expected_operation_count": sum(
                operation.enabled for setup in (job.plan.setups if job.plan else [])
                for operation in setup.operations
            ),
            "current_operation": None, "next_operation_id": None,
            "next_action": "advance_current_operation",
        }
    return _public_l32_rolling_loop(payload)


@app.get("/api/v1/jobs/{job_id}/agent/l32/repair-options")
def get_l32_agent_repair_options(job_id: str) -> dict[str, object]:
    """Return compact, safety-gated repair candidates from the latest real validation."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="L32 修正候选仅适用于 L32 任务")
    directory = job_directory(job_id)
    incremental = _optional_job_json(directory, "agent-l32-incremental-trial.json") or {}
    repair = incremental.get("repair") or {}
    candidates = incremental.get("repair_candidates") or []
    selection = _optional_job_json(directory, "agent-l32-repair-selection.json") or {}
    operation_ids = {
        operation.id for setup in (job.plan.setups if job.plan else [])
        for operation in setup.operations if operation.enabled
    }
    selected_operation_id = str(selection.get("operation_id") or "")
    if (
        repair.get("diagnosis", {}).get("defect") == "missing_back_face_process"
        and selection.get("status") == "applied_validated"
        and selected_operation_id in operation_ids
    ):
        sweep = selection.get("sweep") or {}
        sweep_check = sweep.get("check") or {}
        operation_review = selection.get("operation_review") or {}
        evidence = operation_review.get("evidence") or {}
        return {
            "schema_version": "1.0.0", "job_id": job_id,
            "status": "resolved",
            "diagnosis": repair.get("diagnosis") or {},
            "decision": "applied_validated",
            "next_action": selection.get("next_action") or "reset_and_advance_operation_loop",
            "candidates": [],
            "selected_repair": {
                "candidate_id": selection.get("candidate_id"),
                "operation_id": selected_operation_id,
                "sweep_status": sweep.get("status"),
                "target_contact_mm3": sweep_check.get("maximum_target_contact_mm3"),
                "removed_volume_mm3": evidence.get("removed_volume_delta_mm3"),
                "operation_review_status": operation_review.get("status"),
                "remaining_evidence": selection.get("missing_evidence") or [],
            },
            "release_status": "DRAFT", "production_ready": False,
        }
    return {
        "schema_version": "1.0.0", "job_id": job_id,
        "status": repair.get("status") or "not_available",
        "diagnosis": repair.get("diagnosis") or {},
        "decision": repair.get("decision"),
        "next_action": repair.get("next_action") or incremental.get("next_action"),
        "candidates": [
            {
                "id": item.get("id"), "kind": item.get("kind"),
                "operation_ids": item.get("operation_ids") or [],
                "auto_applicable": bool(item.get("auto_applicable")),
                "capability_available": item.get("capability_available"),
                "validation_status": item.get("validation_status"),
                "reason": item.get("reason"),
                "required_evidence": item.get("required_evidence") or [],
                "strategy_id": item.get("strategy_id"),
                "intent": item.get("intent"),
                "dependencies": item.get("dependencies") or [],
                "validation_requirements": item.get("validation_requirements") or [],
                "parameter_basis": item.get("parameter_basis") or {},
            }
            for item in candidates if isinstance(item, dict)
        ],
        "release_status": "DRAFT", "production_ready": False,
    }


def _check_back_live_face_sweep(
    job_id: str, plan: ProcessPlan,
) -> dict[str, object]:
    directory = job_directory(job_id)
    job = load_job(job_id)
    source = directory / job.filename
    if not source.is_file() or source.suffix.lower() not in {".stp", ".step"}:
        raise ValueError("背面动力刀具扫掠需要原始 STEP 实体")
    rotational = RotationalFeatureAnalysis.model_validate_json(
        (directory / "rotational-features.json").read_text(encoding="utf-8")
    )
    profile_id = str(plan.stock.get("rotational_profile_id") or "")
    profile = next((item for item in rotational.profiles if item.id == profile_id), None)
    axis = next((item for item in rotational.axes if profile and item.id == profile.axis_id), None)
    if profile is None or axis is None or axis.review_state != "accepted":
        raise ValueError("背面动力刀具扫掠需要已确认的回转轴与轮廓")
    back_faces = operations_for_role(plan, "back_face_finish")
    separations = operations_for_role(plan, "material_separation")
    if len(back_faces) != 1 or len(separations) != 1:
        raise ValueError("背面动力刀具扫掠需要唯一的背面精加工与材料分离工序")
    operation = back_faces[0][1]
    cutoff = separations[0][1]
    if operation.type != "back_live_face_finishing":
        raise ValueError("当前背面精加工策略不是动力刀具端面精加工")
    parameters = {
        "finished_back_z_mm": float(cutoff.parameters["finished_back_datum_z_mm"]),
        "axial_stock_mm": float(operation.parameters["stock_allowance_mm"]),
        "stock_radius_mm": float(plan.stock["diameter_mm"]) / 2,
        "tool_diameter_mm": operation.tool.diameter_mm,
        "step_over_mm": float(operation.parameters["step_over_mm"]),
    }
    signature = cached_preview_signature(
        "l32-back-live-face-sweep-v1", source,
        {"axis": axis.model_dump(mode="json"), "parameters": parameters},
    )
    cache_path = directory / f"l32-preview-cache-back-live-face-{signature[:20]}.json"
    cached = read_cached_preview(cache_path, signature)
    if isinstance(cached, dict):
        return cached
    completed = run_freecad_adapter(
        FREECAD_CMD, APP_ROOT / "cam" / "l32_back_live_face_sweep.py",
        [
            source,
            json.dumps(axis.model_dump(mode="json"), separators=(",", ":")),
            json.dumps(parameters, separators=(",", ":")),
        ],
        timeout_seconds=180,
    )
    line = next(
        item.split("CNC_BACK_LIVE_FACE_SWEEP ", 1)[1]
        for item in completed.stdout.splitlines()
        if "CNC_BACK_LIVE_FACE_SWEEP " in item
    )
    check = json.loads(line)
    payload = {
        "schema_version": "1.0.0", "job_id": job_id,
        "operation_id": operation.id, "signature": signature,
        "status": check.get("status"), "check": check,
        "release_status": "DRAFT", "production_ready": False,
    }
    write_cached_preview(cache_path, signature, payload)
    return payload


@app.get("/api/v1/jobs/{job_id}/l32/back-live-face-sweep-check")
def get_l32_back_live_face_sweep_check(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="需要 L32 工艺方案")
    try:
        return _check_back_live_face_sweep(job_id, job.plan)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(status_code=504, detail="背面动力刀具三维扫掠超时") from error
    except (OSError, subprocess.CalledProcessError, StopIteration, ValueError, KeyError) as error:
        raise HTTPException(status_code=422, detail=f"背面动力刀具三维扫掠失败: {error}") from error


@app.post("/api/v1/jobs/{job_id}/agent/l32/repair-options/{candidate_id}/select")
def select_l32_agent_repair_option(
    job_id: str, candidate_id: str, confirmed: bool = False,
) -> dict[str, object]:
    """Select a bounded repair candidate; mutate the plan only after a deterministic gate."""
    if not confirmed:
        raise HTTPException(status_code=422, detail="修正候选必须显式确认")
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="需要完整的 L32 任务上下文")
    options = get_l32_agent_repair_options(job_id)
    candidate = next((
        item for item in options.get("candidates", [])
        if isinstance(item, dict) and item.get("id") == candidate_id
    ), None)
    if candidate is None:
        raise HTTPException(status_code=404, detail="修正候选不存在或已经过期")
    directory = job_directory(job_id)
    selection: dict[str, object] = {
        "schema_version": "1.0.0", "job_id": job_id,
        "candidate_id": candidate_id, "candidate": candidate,
        "selected_at": utc_now(), "release_status": "DRAFT", "production_ready": False,
    }
    if candidate_id == "back_live_tool_face_finish":
        snapshot = load_machine_snapshot(directory / "machine-configuration.json")
        try:
            strategy = get_process_strategy(candidate_id)
            application = strategy.apply(
                plan=job.plan, snapshot=snapshot, material_name=job.material,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=f"工艺策略不适用: {error}") from error
        candidate_plan = application.plan
        operation_id = application.operation_id
        separation_id = application.role_bindings["material_separation"]
        cutoff = next(
            operation for setup in candidate_plan.setups for operation in setup.operations
            if operation.id == separation_id
        )
        selection["strategy"] = application.candidate.model_dump(mode="json")
        selection["role_bindings"] = application.role_bindings
        selection["derived_parameters"] = application.derived_parameters

        candidate_job = job.model_copy(deep=True)
        candidate_job.plan = candidate_plan
        apply_l32_machine_configuration(candidate_job, snapshot)
        if not next(
            item for setup in candidate_plan.setups for item in setup.operations
            if item.id == operation_id
        ).enabled:
            raise HTTPException(status_code=409, detail="设备门禁未允许背面动力刀具工序")
        try:
            sweep = _check_back_live_face_sweep(job_id, candidate_plan)
            if sweep.get("status") != "passed":
                raise ValueError("三维 STEP 刀具扫掠未通过")
            rotational = RotationalFeatureAnalysis.model_validate_json(
                (directory / "rotational-features.json").read_text(encoding="utf-8")
            )
            profile = next((
                item for item in rotational.profiles
                if item.id == candidate_plan.stock.get("rotational_profile_id")
                and item.review_state == "accepted"
            ), None)
            if profile is None:
                raise ValueError("缺少已确认的正式回转轮廓")
            z_values = [point.z for point in profile.points]
            length = max(max(z_values) - min(z_values), 1.0)
            request = WholePartDraftRequest(
                machine_instance_id=snapshot.instance.id,
                source_profile_id=profile.id,
                stock_radius_mm=float(candidate_plan.stock["diameter_mm"]) / 2,
                resolution_mm=0.1,
                approach_z_mm=max(z_values) + 2,
                pickoff_z_mm=min(max(z_values) - 0.2, float(cutoff.parameters["z_mm"]) + length * 0.6),
                grip_length_mm=min(max(length * 0.3, 0.5), 8),
                synchronization_rpm=1200,
                sub_spindle_clamp_confirmed=True,
            )
            result = compile_whole_part_draft(
                job_id, request, candidate_plan, profile, snapshot,
            )
        except (OSError, subprocess.CalledProcessError, StopIteration, ValueError, KeyError) as error:
            raise HTTPException(status_code=422, detail=f"背面动力刀具候选验证失败: {error}") from error

        # The candidate remains isolated from the persisted job until every
        # deterministic and agent-facing operation gate has passed.  Candidate
        # artifacts are useful evidence even when the final gate rejects it,
        # but a failed audit must never leave the formal process plan half
        # updated.
        invalidate_cam_artifacts(directory)
        write_json(directory / "turning-whole-program-ir.json", result.toolpath.model_dump(mode="json"))
        write_json(directory / "turning-whole-program-timeline.json", result.timeline.model_dump(mode="json"))
        write_json(directory / "turning-continuous-simulation.json", result.continuous_simulation.model_dump(mode="json"))
        write_json(directory / "turning-whole-program-draft.json", result.model_dump(mode="json"))
        write_json(directory / "l32-back-live-face-sweep.json", sweep)
        # A repair candidate is accepted on its own evidence boundary.  Running
        # the full sequential reviewer here would stop at any older warning
        # (for example OP20 profile incompleteness) before it ever reaches the
        # newly generated operation.  Keep the whole-program simulation as the
        # shared material state, but review only the operation introduced by
        # this repair.
        candidate_result = result.model_copy(deep=True)
        candidate_result.stages = [
            stage for stage in result.stages if stage.operation_id == operation_id
        ]
        if len(candidate_result.stages) != 1:
            raise HTTPException(status_code=422, detail=f"{operation_id} 工序阶段证据不完整")
        execution = _run_l32_operation_execution_agent(job_id, candidate_job, candidate_result)
        operation_record = next((
            item for item in execution.get("records", []) if item.get("operation_id") == operation_id
        ), {})
        if operation_record.get("status") != "passed":
            raise HTTPException(status_code=422, detail=f"{operation_id} 智能体工序审核未通过")
        sweep_check = sweep.get("check") or {}
        contract = evaluate_validation_contract(application.candidate, {
            "machine_capability": ValidationOutcome(
                validator="machine_capability", status="passed",
                evidence={"capability": strategy.capability, "machine_instance_id": snapshot.instance.id},
            ),
            "operation_dependency": ValidationOutcome(
                validator="operation_dependency",
                status="passed" if float(cutoff.parameters.get("back_face_allowance_mm", 0)) > 0 else "failed",
                evidence={"operation_id": cutoff.id, "retained_stock_mm": cutoff.parameters.get("back_face_allowance_mm")},
            ),
            "continuous_stock": ValidationOutcome(
                validator="continuous_stock", status=result.continuous_simulation.status,
                evidence={"source": "turning-continuous-simulation.json"},
            ),
            "tool_sweep": ValidationOutcome(
                validator="tool_sweep", status=str(sweep.get("status") or "failed"),
                evidence={"sample_count": sweep_check.get("cutter_sample_count")},
            ),
            "target_protection": ValidationOutcome(
                validator="target_protection",
                status="passed" if float(sweep_check.get("maximum_target_contact_mm3") or 0) <= 1e-9 else "failed",
                evidence={"maximum_target_contact_mm3": sweep_check.get("maximum_target_contact_mm3")},
            ),
            "operation_evidence": ValidationOutcome(
                validator="operation_evidence", status=operation_record.get("status", "failed"),
                evidence={
                    "operation_id": operation_id,
                    "removed_volume_mm3": (operation_record.get("evidence") or {}).get("removed_volume_delta_mm3"),
                },
            ),
        })
        if contract.draft_status != "passed":
            raise HTTPException(
                status_code=422,
                detail="工艺策略验证契约未通过: " + ", ".join(contract.missing_draft_evidence),
            )
        job.plan = candidate_plan
        apply_l32_machine_configuration(job, snapshot)
        save_job(directory, job)
        selection.update({
            "status": "applied_validated", "plan_modified": True,
            "operation_id": operation_id, "sweep": sweep,
            "operation_review": operation_record,
            "validation_contract": contract.model_dump(mode="json"),
            "missing_evidence": contract.missing_production_evidence,
            "next_action": "reset_and_advance_operation_loop",
        })
        write_json(directory / "agent-l32-repair-selection.json", selection)
        publish_job_event(
            job_id, "l32_repair", "背面动力刀具修复已通过连续余料与三维扫掠验证", 99,
            agent_node="validate_back_live_face_repair", agent_title="L32 背面端面修复",
            agent_kind="validation", agent_status="completed",
            operation_id=operation_id,
            evidence=[
                {"label": "三维扫掠", "value": sweep.get("status")},
                {"label": "连续余料", "value": result.continuous_simulation.status},
                {"label": "智能体审核", "value": operation_record.get("status")},
            ],
        )
        return selection
    if not candidate.get("auto_applicable"):
        selection.update({
            "status": "selected_pending_evidence",
            "plan_modified": False,
            "missing_evidence": candidate.get("required_evidence") or [],
            "next_action": "collect_candidate_evidence",
        })
        write_json(directory / "agent-l32-repair-selection.json", selection)
        publish_job_event(
            job_id, "l32_repair", f"已选择修正候选 {candidate_id}，等待补充安全证据", 99,
            agent_node="select_repair_candidate", agent_title="L32 工艺修正候选",
            agent_kind="decision", agent_status="waiting",
            evidence=[
                {"label": "候选", "value": candidate_id},
                {"label": "待补证据", "value": len(selection["missing_evidence"])},
            ],
        )
        return selection

    if candidate_id != "restore_op50_back_turning":
        raise HTTPException(status_code=409, detail="该自动修正尚无确定性应用器")
    if job.plan.stock.get("nonrotational_turning_limit_z_mm") is not None:
        raise HTTPException(status_code=409, detail="背面非回转保护区禁止恢复 OP50 端面车削")
    snapshot = load_machine_snapshot(directory / "machine-configuration.json")
    if "back_turning" not in snapshot.validation.capabilities:
        raise HTTPException(status_code=409, detail="绑定设备未确认 back_turning 能力")
    rebuilt = build_process_plan(
        job.analysis, material=job.material, machine=job.machine,
        requirements=job.plan.manufacturing_requirements,
    )
    template = next((
        operation.model_copy(deep=True)
        for setup in rebuilt.setups for operation in setup.operations
        if operation.id == "OP50"
    ), None)
    target_setup = next((setup for setup in job.plan.setups if setup.id == "SETUP-L32-SUB"), None)
    if template is None or target_setup is None:
        raise HTTPException(status_code=409, detail="无法从当前几何重建安全的 OP50 模板")
    if not any(operation.id == "OP50" for operation in target_setup.operations):
        target_setup.operations.append(template)
        target_setup.operations.sort(key=lambda operation: operation.sequence)
    apply_l32_machine_configuration(job, snapshot)
    op50 = next(operation for operation in target_setup.operations if operation.id == "OP50")
    if not op50.enabled:
        raise HTTPException(status_code=409, detail="OP50 在设备与几何门禁后仍不可启用")
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    invalidate_cam_artifacts(directory)
    selection.update({
        "status": "applied", "plan_modified": True,
        "operation_id": "OP50", "next_action": "recompile_and_simulate",
    })
    write_json(directory / "agent-l32-repair-selection.json", selection)
    return selection


@app.post("/api/v1/jobs/{job_id}/agent/l32/loop/advance")
def advance_l32_agent_rolling_loop(
    job_id: str, reset: bool = False, refresh: bool = False,
) -> dict[str, object]:
    """Accept at most one L32 operation after checking real continuous-stock evidence."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="滚动工序闭环目前仅适用于 L32 任务")
    if job.status != "completed" or not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="需要已完成的几何分析与 L32 工艺草案")
    with L32_ROLLING_LOOP_LOCK:
        if job_id in L32_ROLLING_LOOP_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在推进滚动工序闭环")
        L32_ROLLING_LOOP_JOBS.add(job_id)
    directory = job_directory(job_id)
    loop_path = directory / "agent-l32-rolling-loop.json"
    try:
        from .agent.l32_rolling_loop import advance_l32_rolling_loop, plan_signature

        plan_payload = job.plan.model_dump(mode="json")
        signature = plan_signature(plan_payload)
        previous = _optional_job_json(directory, loop_path.name) or {}
        cached_validation = previous.get("validation_cache") if isinstance(previous, dict) else None
        cache_valid = (
            not reset
            and not refresh
            and previous.get("plan_signature") == signature
            and isinstance(cached_validation, dict)
        )
        if cache_valid:
            validation = cached_validation
        else:
            validation = validate_l32_agent_plan(job_id)
            if reset or previous.get("plan_signature") != signature:
                previous = None

        result = advance_l32_rolling_loop(
            job_id=job_id,
            plan=plan_payload,
            validation=validation,
            previous=previous,
            reset=reset,
        )
        result["validation_cache"] = validation
        result["evidence_reused"] = cache_valid
        write_json(loop_path, result)
        decision = result.get("decision") or {}
        publish_job_event(
            job_id, "l32_rolling_operation",
            str(result.get("message") or "L32 滚动工序闭环已更新"),
            min(99.0, 5.0 + 90.0 * float(result.get("accepted_count") or 0)
                / max(float(result.get("expected_operation_count") or 1), 1.0)),
            agent_node="rolling_operation_gate", agent_title="逐工序规划与仿真审核",
            agent_kind="validation",
            agent_status="completed" if decision.get("decision") == "accepted" else "blocked",
            operation_id=decision.get("operation_id"),
            evidence=[
                {"label": "门禁结论", "value": decision.get("decision")},
                {"label": "已接受工序", "value": result.get("accepted_count")},
                {"label": "下一动作", "value": result.get("next_action")},
            ],
        )
        return _public_l32_rolling_loop(result)
    finally:
        with L32_ROLLING_LOOP_LOCK:
            L32_ROLLING_LOOP_JOBS.discard(job_id)


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


def _agent_evaluation_block_reason(directory: Path) -> str | None:
    payload = _optional_job_json(directory, "agent-evaluation.json")
    if not payload or payload.get("eligible_for_promotion") is not False:
        return None
    reasons = [
        str(item) for item in payload.get("blocking_reasons", [])
        if isinstance(item, str) and item.strip()
    ]
    return "；".join(reasons)[:500] if reasons else "智能体候选方案未通过制造风险门禁"


def provision_default_l32_planning_instance(job: JobResponse, directory: Path) -> None:
    """Attach a deterministic L32 configuration so a new L32 job can compile CAM immediately."""
    if job.device_id != "citizen-cincom-l32" or job.machine_instance_id or not job.plan:
        return
    stock_diameter = float(job.plan.stock.get("diameter_mm", 0) or 0)
    if stock_diameter > 38 + 1e-9:
        job.plan.warnings.append("棒料直径超过 L32 Ø38 上限，无法建立任务设备配置。")
        return
    uses_38mm_option = stock_diameter > 32 + 1e-9
    instance = MachineInstance(
        id=f"l32-{job.id[:8]}",
        definition_id="citizen-cincom-l32",
        name=f"L32 VIII · {job.id[:8]}",
        controller_revision="M70LPC-VU",
        operation_mode="guide_bushing",
        variant="VIII",
        installed_modules=["U30B", "U151B"],
        enabled_options=["bar_diameter_38mm"] if uses_38mm_option else [],
        bar_diameter_mm=38 if uses_38mm_option else 32,
    )
    snapshot = snapshot_l32_instance(instance)
    if not snapshot.validation.valid:
        raise ValueError("default L32 planning instance is invalid")
    apply_l32_machine_configuration(job, snapshot)
    instance_path = machine_instance_path(instance.id)
    instance_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(instance_path, snapshot.model_dump(mode="json"))
    write_json(directory / "machine-configuration.json", snapshot.model_dump(mode="json"))


@app.post("/api/v1/jobs/{job_id}/machine-instance/default", response_model=JobResponse)
def bind_default_l32_planning_instance(job_id: str) -> JobResponse:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or not job.plan:
        raise HTTPException(status_code=409, detail="Default L32 configuration requires a completed L32 process plan")
    directory = job_directory(job_id)
    provision_default_l32_planning_instance(job, directory)
    if not job.machine_instance_id:
        raise HTTPException(status_code=422, detail="The job cannot use the default L32 configuration")
    reapply_bound_l32_machine_configuration(job, directory)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


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
    analysis_path = directory / "analysis.json"
    model_path = directory / "model.stl"

    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, percent, **details)

    try:
        report(
            "geometry_analysis", "正在解析 STEP 拓扑与制造特征", 12,
            agent_kind="tool_call", agent_status="running",
            agent_title="读取并解析三维几何",
        )
        analysis = run_geometry_analyzer(source_path, analysis_path, model_path)
        rotational_analysis = persist_rotational_analysis(directory, job, analysis)
        feature_count = (
            len(analysis.planar_features) + len(analysis.cylindrical_features)
            + len(analysis.prismatic_features) + len(analysis.planar_machining_features)
            + len(analysis.internal_profile_features)
        )
        job.analysis = analysis
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
        save_job(directory, job)
        report(
            "geometry_analysis", "三维几何分析完成", 34,
            agent_kind="tool_result", agent_status="completed",
            feature_count=feature_count,
            hole_count=sum(item.kind == "hole" and item.review_state != "excluded" for item in analysis.cylindrical_features),
            rotational_status=rotational_analysis.status if rotational_analysis else None,
            model_url=job.model_url,
            evidence=[
                {"label": "制造特征", "value": feature_count},
                {"label": "孔特征", "value": sum(item.kind == "hole" and item.review_state != "excluded" for item in analysis.cylindrical_features)},
            ],
            artifacts=[
                {"id": "model", "label": "三维模型", "kind": "model", "url": job.model_url},
                {"id": "analysis", "label": "几何分析", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/analysis.json"},
            ],
        )

        report(
            "draft_planning", "正在生成确定性工艺草案", 40,
            agent_kind="activity", agent_status="running",
            agent_title="构建可验证的工艺草案",
            viewer={"kind": "model", "url": job.model_url},
        )
        plan = build_process_plan(analysis, material=job.material, machine=job.machine)
        if ai_assisted:
            ai_progress_by_phase = {
                "ai_context": 43,
                "ai_request": 46,
                "ai_waiting": 49,
                "ai_stream_manufacturing_intent": 52,
                "ai_stream_part_family": 55,
                "ai_stream_recommended_process_kind": 58,
                "ai_stream_deterministic_plan_assessment": 61,
                "ai_stream_setup_strategy": 64,
                "ai_stream_route_recommendations": 67,
                "ai_stream_operation_recommendations": 70,
                "ai_stream_risks": 73,
                "ai_stream_missing_information": 76,
                "ai_stream_requires_engineer_review": 79,
                "ai_response": 81,
                "ai_schema_validation": 83,
                "ai_capability_validation": 85,
                "ai_review_completed": 87,
                "agent_review": 44,
                "agent_synthesis": 88,
                "agent_validation": 89,
                "agent_operation_audit_prepare": 89,
                "agent_operation_audit": 89,
                "agent_operation_audit_summary": 90,
                "agent_replan_from_audit": 90,
                "agent_decision": 90,
                "agent_fallback": 90,
            }
            ai_progress_percent = 40

            def report_ai_progress(stage: str, message: str, **details: object) -> None:
                nonlocal ai_progress_percent
                target_percent = ai_progress_by_phase.get(stage, ai_progress_percent + 1)
                ai_progress_percent = min(90, max(ai_progress_percent, target_percent))
                report(
                    "ai_planning",
                    "AI 正在判断制造意图、装夹路线与工序策略",
                    ai_progress_percent,
                    agent_kind="reasoning", agent_status="running",
                    agent_title="AI 工艺研判",
                    detail=message,
                    phase=stage,
                    viewer={"kind": "model", "url": job.model_url},
                    **details,
                )

            from .agent.config import load_agent_settings
            from .agent.planning_graph import run_process_planning_subgraph

            agent_settings = load_agent_settings()
            if agent_settings.enabled:
                agent_result = run_process_planning_subgraph(
                    job_id=job_id,
                    analysis=analysis,
                    baseline_plan=plan,
                    material=job.material,
                    machine=job.machine,
                    settings=agent_settings,
                    progress_callback=report_ai_progress,
                )
            else:
                try:
                    guidance = review_process_plan(
                        analysis, plan, progress_callback=report_ai_progress,
                    )
                    review = guidance.get("review", {})
                    legacy_candidate = plan.model_copy(deep=True)
                    legacy_candidate.ai_planning = {
                        "provider": guidance.get("provider"),
                        "model": guidance.get("model"),
                        "created_at": guidance.get("created_at"),
                        "manufacturing_intent": review.get("manufacturing_intent"),
                        "recommended_process_kind": review.get("recommended_process_kind"),
                        "part_family": review.get("part_family"),
                        "confidence": float(review.get("confidence", 0) or 0),
                        "summary": review.get("summary"),
                        "requires_engineer_review": review.get("requires_engineer_review", False),
                        "agent_mode": "disabled",
                    }
                    agent_result = {
                        "status": "blocked", "guidance": guidance,
                        "candidate_plan": legacy_candidate.model_dump(mode="json"),
                        "evaluation": {
                            "schema_version": "1.0.0", "mode": "disabled",
                            "eligible_for_promotion": False,
                            "production_result_changed": False,
                            "blocking_reasons": ["LangGraph 智能体编排已禁用"],
                        },
                    }
                except QwenPlanningError as error:
                    agent_result = {
                        "status": "fallback", "error": str(error),
                        "candidate_plan": plan.model_dump(mode="json"),
                        "evaluation": {
                            "schema_version": "1.0.0", "mode": "disabled",
                            "eligible_for_promotion": False,
                            "production_result_changed": False,
                            "blocking_reasons": [f"AI 研判不可用：{error}"],
                        },
                    }
            guidance = agent_result.get("guidance")
            evaluation = agent_result.get("evaluation", {})
            candidate_payload = agent_result.get("candidate_plan")
            operation_audit = agent_result.get("operation_audit")
            if guidance:
                write_json(directory / "planning-guidance.json", guidance)
            if candidate_payload:
                write_json(directory / "agent-plan.json", candidate_payload)
            if operation_audit:
                write_json(directory / "operation-audit.json", operation_audit)
            write_json(directory / "agent-evaluation.json", evaluation)

            if agent_result.get("status") == "fallback" or not guidance:
                error_message = str(agent_result.get("error") or "AI 研判未产生有效候选方案")
                if agent_settings.writes_production_results:
                    raise ValueError(
                        "AI 智能体规划失败，主动规划模式不会回退到旧规则方案："
                        + error_message
                    )
                plan.ai_planning = {
                    "status": "fallback", "message": error_message,
                    "agent_mode": agent_settings.mode,
                }
                plan.warnings.append("AI 辅助规划不可用，本次已回退到确定性规则规划")
                report(
                    "ai_integration", "AI 暂不可用，已自动回退到规则规划", 90,
                    agent_kind="warning", agent_status="completed", warning=error_message,
                    evidence=[{"label": "回退策略", "value": "确定性规则规划"}],
                    artifacts=[{
                        "id": "agent-evaluation", "label": "智能体评测", "kind": "json",
                        "url": f"/api/v1/jobs/{job_id}/files/agent-evaluation.json",
                    }],
                )
            else:
                review = guidance.get("review", {})
                recommended_kind = str(review.get("recommended_process_kind", ""))
                confidence = float(review.get("confidence", 0) or 0)
                candidate = ProcessPlan.model_validate(candidate_payload)
                promoted = bool(evaluation.get("production_result_changed"))
                selected_as_primary = bool(evaluation.get("primary_plan_selected"))
                if (selected_as_primary or promoted) and bool(evaluation.get("eligible_for_promotion")):
                    plan = candidate
                else:
                    plan.ai_planning = candidate.ai_planning
                report(
                    "ai_integration",
                    (
                        "AI 智能体方案已成为当前工作方案，等待确定性校验与工程师审批"
                        if selected_as_primary
                        else "智能体候选方案已完成影子评测，正式方案保持不变"
                    ),
                    90,
                    agent_kind="decision", agent_status="completed",
                    agent_title="候选方案晋级判断",
                    manufacturing_intent=review.get("manufacturing_intent"),
                    recommended_process_kind=recommended_kind,
                    confidence=confidence,
                    agent_mode=agent_settings.mode,
                    eligible_for_promotion=bool(evaluation.get("eligible_for_promotion")),
                    production_result_changed=promoted,
                    primary_plan_selected=selected_as_primary,
                    evidence=[
                        {"label": "制造意图", "value": review.get("manufacturing_intent")},
                        {"label": "建议工艺类型", "value": recommended_kind},
                        {"label": "置信度", "value": confidence},
                        {"label": "运行模式", "value": agent_settings.mode},
                        {"label": "是否可晋级", "value": bool(evaluation.get("eligible_for_promotion"))},
                        {"label": "当前工作方案", "value": "AI 智能体" if selected_as_primary else "确定性基线"},
                    ],
                    artifacts=[
                        {"id": "planning-guidance", "label": "AI 规划建议", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/planning-guidance.json"},
                        {"id": "agent-plan", "label": "智能体候选方案", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/agent-plan.json"},
                        {"id": "agent-evaluation", "label": "候选方案评测", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/agent-evaluation.json"},
                        *([{
                            "id": "operation-audit", "label": "逐工序能力校验", "kind": "json",
                            "url": f"/api/v1/jobs/{job_id}/files/operation-audit.json",
                        }] if operation_audit else []),
                    ],
                )

        report(
            "process_generation", "正在生成装夹、工序、刀具与切削参数", 92,
            agent_kind="tool_call", agent_status="running",
            viewer={"kind": "model", "url": job.model_url},
        )
        plan.coverage = evaluate_plan_coverage(analysis, plan)
        plan.manufacturing_route = build_manufacturing_route(analysis, plan)
        plan.knowledge_assessment = assess_plan_knowledge(analysis, plan)
        operation_count = sum(len(setup.operations) for setup in plan.setups)
        report(
            "coverage_validation", "正在检查工艺覆盖率和 CAM 能力", 96,
            agent_kind="validation", agent_status="completed",
            setup_count=len(plan.setups), operation_count=operation_count,
            coverage_score=plan.coverage.score if plan.coverage else None,
            evidence=[
                {"label": "装夹数", "value": len(plan.setups)},
                {"label": "工序数", "value": operation_count},
                {"label": "覆盖率", "value": plan.coverage.score if plan.coverage else None},
            ],
        )

        # Keep the planning stream open until the supported CAM/stock validation
        # finishes. A plan is not the same thing as a verified simulation.
        job.status = "processing" if ai_assisted else "completed"
        job.analysis = analysis
        job.plan = plan
        provision_default_l32_planning_instance(job, directory)
        write_json(directory / "plan.json", plan.model_dump(mode="json"))
        persist_rotational_analysis(directory, job, analysis)
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
        from .agent.world_model import create_manufacturing_world_model
        world = create_manufacturing_world_model(
            job_id=job_id,
            analysis=analysis,
            plan=plan,
            material=job.material,
            machine=job.machine,
            filename=job.filename,
            model_url=job.model_url,
            require_initial_perception=True,
        )
        from .agent.config import load_agent_settings as load_world_agent_settings
        from .agent.orchestrator import OrchestratorTools, run_manufacturing_orchestrator
        world_agent_settings = load_world_agent_settings()
        if world_agent_settings.enabled:
            orchestrator_result = run_manufacturing_orchestrator(
                world=world,
                settings=world_agent_settings,
                tools=OrchestratorTools(),
                max_steps=8,
                progress_callback=lambda stage, message, **details: report(
                    "orchestrator",
                    message,
                    98,
                    agent_kind="decision" if stage == "orchestrator_decide" else "tool_call",
                    agent_status="completed" if stage == "orchestrator_decide" else "waiting",
                    agent_title="制造任务协调",
                    **details,
                ),
            )
            world = type(world).model_validate(orchestrator_result["world"])
            write_json(directory / "agent-orchestrator.json", {
                "schema_version": "1.0.0",
                "job_id": job_id,
                "status": orchestrator_result.get("status"),
                "next_action": orchestrator_result.get("action"),
                "trace": orchestrator_result.get("trace", []),
            })
        write_json(directory / "agent-world-model.json", world.model_dump(mode="json"))
        save_job(directory, job)
        cam_validation_outcome = "not_requested"
        if ai_assisted:
            if (
                plan.process_kind == "subtractive"
                and job.device_id == "citizen-cincom-l32"
                and plan.automation_status != "unsupported"
            ):
                report(
                    "cam_validation", "正在编译 L32 整件刀路，并按工序验证连续余料", 97,
                    agent_kind="tool_call", agent_status="running", agent_title="L32 逐工序执行与审核",
                )
                try:
                    l32_result = _create_l32_whole_program_with_agent_loop(job_id)
                    execution = _optional_job_json(directory, "agent-l32-execution.json") or {}
                    execution_status = str(execution.get("status") or "blocked")
                    simulation_status = l32_result.continuous_simulation.status
                    cam_validation_outcome = (
                        "failed" if simulation_status == "failed" or execution_status == "blocked"
                        else "warning" if execution_status == "action_required"
                        else "passed"
                    )
                    report(
                        "cam_validation",
                        "L32 刀路、连续余料与逐工序审核已归档" if cam_validation_outcome != "failed" else "L32 逐工序验证未通过，请检查阻断原因",
                        99,
                        agent_kind="validation",
                        agent_status="blocked" if cam_validation_outcome == "failed" else "completed",
                        agent_title="L32 仿真证据已归档",
                        evidence=[
                            {"label": "连续余料", "value": simulation_status},
                            {"label": "逐工序审核", "value": execution_status},
                            {"label": "已审核工序", "value": len(l32_result.stages)},
                        ],
                        viewer={"kind": "model", "url": job.model_url, "mode": "仿真"},
                    )
                except Exception as l32_error:
                    cam_validation_outcome = "failed"
                    detail = getattr(l32_error, "detail", None) or str(l32_error)
                    report(
                        "cam_validation", f"L32 刀路或逐工序仿真未完成：{detail}", 99,
                        agent_kind="error", agent_status="failed", agent_title="L32 仿真失败",
                        evidence=[{"label": "原因", "value": str(detail)[:500]}],
                    )
            elif plan.process_kind == "subtractive" and plan.automation_status != "unsupported" and resolve_executable(FREECAD_CMD):
                report(
                    "cam_validation", "工艺草案已保存，正在生成真实刀路并验证累计余料", 97,
                    agent_kind="tool_call", agent_status="running", agent_title="逐工序刀路与仿真",
                )

                def report_cam(event: dict[str, object]) -> None:
                    stage = str(event.get("stage", "cam"))
                    operation_id = event.get("operation_id")
                    report(
                        "cam_validation", str(event.get("message", "正在验证刀路与材料状态")),
                        min(99, 97 + float(event.get("percent", 0) or 0) * 0.02),
                        agent_kind="validation" if stage == "completed" else "tool_result" if stage in {"simulation", "collision", "preflight"} else "tool_call",
                        agent_status="completed" if stage == "completed" else "running",
                        agent_title="逐工序刀路与仿真", agent_node=stage,
                        operation_id=operation_id,
                        viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"} if operation_id else {"kind": "model", "url": job.model_url},
                        current=event.get("current"), total=event.get("total"),
                    )

                try:
                    cam_validation = _create_cam_with_agent_loop(job_id, progress_callback=report_cam)
                    execution_status = (cam_validation.get("agent_execution") or {}).get("status")
                    cam_validation_outcome = (
                        "failed" if cam_validation["verification"]["status"] == "failed" or cam_validation["collision"]["status"] == "failed" or execution_status == "blocked"
                        else "warning" if cam_validation["verification"]["status"] == "warning" or execution_status == "action_required"
                        else "passed"
                    )
                    report(
                        "cam_validation", "刀路与仿真证据已归档；部分检查未通过，请复核" if cam_validation_outcome == "failed" else "刀路、累计余料与校验结果已归档；请检查各工序结论", 99,
                        agent_kind="validation", agent_status="blocked" if cam_validation_outcome == "failed" else "completed", agent_title="仿真证据已归档",
                        evidence=[{"label": "验证", "value": cam_validation["verification"]["status"]}, {"label": "碰撞", "value": cam_validation["collision"]["status"]}],
                        artifacts=[
                            {"id": "toolpath", "label": "刀路", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/toolpath.json"},
                            {"id": "simulation", "label": "材料仿真", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/simulation.json"},
                            {"id": "verification", "label": "验证结论", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/verification.json"},
                        ],
                    )
                except Exception as cam_error:
                    cam_validation_outcome = "failed"
                    for partial_name in ("toolpath.json", "simulation.json", "verification.json", "collision.json", "cam-manifest.json"):
                        (directory / partial_name).unlink(missing_ok=True)
                    report(
                        "cam_validation", f"刀路或仿真未完成：{cam_error}", 99,
                        agent_kind="error", agent_status="failed", agent_title="仿真需要人工复核",
                        evidence=[{"label": "原因", "value": str(cam_error)[:500]}],
                    )
            else:
                cam_validation_outcome = "unavailable"
                report(
                    "cam_validation", "当前工艺或运行环境不支持自动仿真，已保留待验证状态", 99,
                    agent_kind="warning", agent_status="waiting", agent_title="仿真待验证",
                )
            # CAM/remediation may have updated the plan. Do not overwrite it with
            # the pre-validation in-memory JobResponse.
            job = load_job(job_id)
            job.status = "completed"
            save_job(directory, job)
        report(
            "completed", "工艺方案已生成，仿真未通过或未完成，请复核" if cam_validation_outcome == "failed" else "工艺方案已生成，仿真待验证" if cam_validation_outcome == "unavailable" else "工艺方案与仿真证据已生成" if cam_validation_outcome in {"passed", "warning"} else "工艺方案已生成", 100,
            agent_kind="result",
            agent_status="blocked" if cam_validation_outcome == "failed" else "waiting" if cam_validation_outcome == "unavailable" else "completed",
            validation_outcome=cam_validation_outcome,
            setup_count=len(plan.setups), operation_count=operation_count,
            coverage_score=plan.coverage.score if plan.coverage else None,
            artifacts=[
                {"id": "model", "label": "三维模型", "kind": "model", "url": job.model_url},
                {"id": "analysis", "label": "几何分析", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/analysis.json"},
                {"id": "plan", "label": "工艺方案", "kind": "json", "url": f"/api/v1/jobs/{job_id}/files/plan.json"},
            ],
            viewer={"kind": "model", "url": job.model_url},
        )
        return job
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        job.status = "failed"
        details = getattr(error, "stderr", None) or str(error)
        job.error = details[-2000:]
        save_job(directory, job)
        report(
            "error", job.error or "工艺生成失败", 100,
            agent_kind="error", agent_status="failed",
            evidence=[{"label": "异常", "value": job.error or str(error)}],
        )
        return job


def _analyze_new_job(
    job_id: str,
    *,
    progress_callback: Callable[..., None] | None = None,
) -> JobResponse:
    """Run deterministic geometry intake only; leave process planning to an external agent."""
    job = load_job(job_id)
    directory = job_directory(job_id)
    source_path = directory / job.filename

    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback(stage, message, percent, **details)

    try:
        report(
            "geometry_analysis", "Harness 正在调用确定性 STEP 几何解析", 15,
            agent_kind="tool_call", agent_status="running",
            agent_title="读取并解析三维几何",
        )
        analysis = run_geometry_analyzer(
            source_path, directory / "analysis.json", directory / "model.stl",
        )
        rotational = persist_rotational_analysis(directory, job, analysis)
        job.analysis = analysis
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
        job.status = "completed"
        save_job(directory, job)
        feature_count = sum(len(items) for items in (
            analysis.planar_features, analysis.cylindrical_features,
            analysis.prismatic_features, analysis.planar_machining_features,
            analysis.internal_profile_features,
        ))
        report(
            "geometry_analysis", "几何解析完成，等待 Harness 制定装夹与工序", 100,
            agent_kind="tool_result", agent_status="completed",
            agent_title="几何证据已就绪",
            feature_count=feature_count,
            rotational_status=rotational.status if rotational else None,
            evidence=[
                {"label": "制造特征", "value": feature_count},
                {"label": "规划所有者", "value": "DeepSeek Harness"},
            ],
            viewer={"kind": "model", "url": job.model_url},
        )
        return job
    except (subprocess.SubprocessError, OSError, ValueError) as error:
        job.status = "failed"
        details = getattr(error, "stderr", None) or str(error)
        job.error = str(details)[-2000:]
        save_job(directory, job)
        report(
            "error", job.error or "几何解析失败", 100,
            agent_kind="error", agent_status="failed",
        )
        return job


@app.post("/api/v1/jobs", response_model=JobResponse)
async def create_job(
    step: UploadFile = File(...),
    material: str = Form("待确认（候选：6061-T6 铝合金）"),
    machine: str = Form("待确认（候选：VMC850 三轴立式加工中心）"),
    device_id: str | None = Form(None),
) -> JobResponse:
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")
    if HARNESS_ONLY_MODE:
        raise HTTPException(
            status_code=409,
            detail="请在 DeepSeek Harness 页面上传 STEP/STP；CNC 仅作为领域工具和仿真后端。",
        )
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

    job = JobResponse(
        id=job_id,
        status="processing",
        filename=filename,
        created_at=utc_now(),
        material=material,
        machine=machine,
        device_id=device_id,
    )
    save_job(directory, job)

    return _process_new_job(job_id)


@app.post("/api/v1/jobs/start", response_model=JobResponse)
async def start_job(
    step: UploadFile = File(...),
    material: str = Form("待确认（候选：6061-T6 铝合金）"),
    machine: str = Form("待确认（候选：VMC850 三轴立式加工中心）"),
    device_id: str | None = Form(None),
) -> JobResponse:
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")
    if HARNESS_ONLY_MODE:
        raise HTTPException(
            status_code=409,
            detail="请在 DeepSeek Harness 页面上传 STEP/STP；CNC 仅作为领域工具和仿真后端。",
        )
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

    job = JobResponse(
        id=job_id,
        status="processing",
        filename=filename,
        created_at=utc_now(),
        material=material,
        machine=machine,
        device_id=device_id,
    )
    save_job(directory, job)
    with JOB_EVENT_CONDITION:
        JOB_EVENT_LOGS[job_id] = []
        write_json(directory / "planning-events.json", [])
    publish_job_progress = lambda stage, message, percent, **details: publish_job_event(
        job_id, stage, message, percent, **details,
    )
    publish_job_progress("uploading", "三维模型上传完成", 6)

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


@app.post("/api/v1/jobs/intake", response_model=JobResponse)
async def intake_job_for_external_agent(
    step: UploadFile = File(...),
    material: str = Form("待确认"),
    machine: str = Form("待确认"),
    device_id: str | None = Form(None),
) -> JobResponse:
    """Upload and analyze STEP geometry without creating a process plan."""
    filename = Path(step.filename or "part.step").name
    if Path(filename).suffix.lower() not in {".step", ".stp"}:
        raise HTTPException(status_code=400, detail="Only STEP/STP files are supported")
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

    job = JobResponse(
        id=job_id, status="processing", filename=filename, created_at=utc_now(),
        material=material, machine=machine, device_id=device_id,
    )
    save_job(directory, job)
    with JOB_EVENT_CONDITION:
        JOB_EVENT_LOGS[job_id] = []
        write_json(directory / "planning-events.json", [])
    progress = lambda stage, message, percent, **details: publish_job_event(
        job_id, stage, message, percent, **details,
    )
    progress(
        "uploading", "Harness 已上传三维模型，工艺规划尚未开始", 6,
        agent_kind="tool_result", agent_status="completed",
        evidence=[{"label": "规划所有者", "value": "DeepSeek Harness"}],
    )

    def worker() -> None:
        _analyze_new_job(job_id, progress_callback=progress)

    threading.Thread(target=worker, name=f"job-intake-{job_id[:8]}", daemon=True).start()
    return job


@app.get("/api/v1/jobs/{job_id}/events")
def stream_job_progress(job_id: str) -> StreamingResponse:
    load_job(job_id)
    with JOB_EVENT_CONDITION:
        if job_id not in JOB_EVENT_LOGS:
            JOB_EVENT_LOGS[job_id] = load_job_events(job_id)

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


@app.get("/api/v1/jobs/{job_id}/planning-events")
def get_job_planning_events(job_id: str) -> list[dict[str, object]]:
    load_job(job_id)
    with JOB_EVENT_CONDITION:
        if job_id in JOB_EVENT_LOGS:
            return list(JOB_EVENT_LOGS[job_id])
    return load_job_events(job_id)


@app.get("/api/v1/agent/architecture")
def get_agent_architecture() -> dict[str, object]:
    return agent_architecture()


def _l32_profile_undercut_spans(profile: object) -> list[dict[str, float]]:
    """Return compact, UI-facing evidence for axial contour reversals."""
    points = list(getattr(profile, "points", []) or [])
    grouped: list[tuple[float, list[float]]] = []
    for point in sorted(points, key=lambda item: (item.z, item.radius)):
        if grouped and abs(grouped[-1][0] - point.z) <= 1e-9:
            grouped[-1][1].append(float(point.radius))
        else:
            grouped.append((float(point.z), [float(point.radius)]))
    side = str(getattr(profile, "side", "outer"))
    nodes = [(z, max(radii) if side == "outer" else min(radii)) for z, radii in grouped]
    ordered = sorted(nodes, reverse=True)
    entered_reduced_diameter = False
    spans: list[dict[str, float]] = []
    for left, right in zip(ordered, ordered[1:]):
        if right[1] < left[1] - 1e-6:
            entered_reduced_diameter = True
        elif entered_reduced_diameter and right[1] > left[1] + 1e-6:
            spans.append({
                "z_start_mm": round(min(left[0], right[0]), 4),
                "z_end_mm": round(max(left[0], right[0]), 4),
                "radius_before_mm": round(left[1], 4),
                "radius_after_mm": round(right[1], 4),
                "radial_change_mm": round(right[1] - left[1], 4),
            })
    return spans


def _build_l32_agent_review_context(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="该审核工作台仅适用于 L32 任务")
    directory = job_directory(job_id)
    rotational_path = directory / "rotational-features.json"
    if not rotational_path.is_file():
        raise HTTPException(status_code=409, detail="尚未生成回转轮廓分析")
    rotational = RotationalFeatureAnalysis.model_validate_json(
        rotational_path.read_text(encoding="utf-8")
    )
    usable_profiles = [item for item in rotational.profiles if item.review_state != "excluded"]
    recommended = next(
        (item for item in usable_profiles if item.review_state == "accepted"),
        max(usable_profiles, key=lambda item: (item.confidence, len(item.points)), default=None),
    )
    trial: dict[str, object] = {}
    trial_path = directory / "agent-l32-incremental-trial.json"
    if trial_path.is_file():
        try:
            loaded = json.loads(trial_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                trial = loaded
        except (OSError, ValueError):
            trial = {}
    candidate_labels = {
        "reverse_backside_feed": "反向背面进给",
        "split_monotonic_backside_regions": "拆分为可达的单调轮廓区域",
        "dedicated_grooving_or_form_tool": "改用切槽刀、成形刀或动力刀具",
        "replace_turning_tool_hand": "更换匹配进给方向的左右手车刀",
        "engineering_review": "工程师复核加工策略",
    }
    candidates = []
    for raw in trial.get("repair_candidates", []) or []:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id", "engineering_review"))
        candidates.append({
            "id": identifier,
            "label": candidate_labels.get(identifier, identifier),
            "kind": raw.get("kind"),
            "operation_ids": raw.get("operation_ids", []),
            "status": raw.get("validation_status", "awaiting_review"),
            "reason": raw.get("reason", "需要补充工程证据后验证"),
            "auto_applicable": bool(raw.get("auto_applicable", False)),
        })
    recommended_undercuts = _l32_profile_undercut_spans(recommended) if recommended else []
    if not candidates and recommended_undercuts:
        candidates = [
            {
                "id": "split_monotonic_backside_regions",
                "label": candidate_labels["split_monotonic_backside_regions"],
                "kind": "operation_split", "operation_ids": [],
                "status": "awaiting_profile_confirmation",
                "reason": "先确认轮廓，再把标准纵向车削限制在刀具可达的单调区间。",
                "auto_applicable": False,
            },
            {
                "id": "dedicated_grooving_or_form_tool",
                "label": candidate_labels["dedicated_grooving_or_form_tool"],
                "kind": "process_change", "operation_ids": [],
                "status": "awaiting_profile_confirmation",
                "reason": "轮廓反转区域可能需要切槽刀、成形刀或动力刀具，确认后再做刀具包络校核。",
                "auto_applicable": False,
            },
        ]
    records = [item for item in trial.get("records", []) or [] if isinstance(item, dict)]
    failed = [item for item in records if item.get("status") == "blocked"]
    profiles = []
    for profile in rotational.profiles:
        point_count = len(profile.points)
        stride = max(1, (point_count + 179) // 180)
        preview_points = profile.points[::stride]
        if profile.points and preview_points[-1] != profile.points[-1]:
            preview_points.append(profile.points[-1])
        z_values = [point.z for point in profile.points]
        radii = [point.radius for point in profile.points]
        profiles.append({
            "id": profile.id,
            "side": profile.side,
            "method": profile.extraction_method,
            "confidence": profile.confidence,
            "review_state": profile.review_state,
            "review_reasons": profile.review_reasons,
            "provisional_decision": (
                profile.provisional_decision.model_dump(mode="json")
                if profile.provisional_decision else None
            ),
            "point_count": point_count,
            "z_min_mm": min(z_values) if z_values else 0,
            "z_max_mm": max(z_values) if z_values else 0,
            "diameter_min_mm": min(radii) * 2 if radii else 0,
            "diameter_max_mm": max(radii) * 2 if radii else 0,
            "points": [{"z": point.z, "radius": point.radius} for point in preview_points],
            "undercut_spans": _l32_profile_undercut_spans(profile),
        })
    blocker = str(trial.get("whole_program_blocker", ""))
    if not blocker and recommended_undercuts:
        blocker = f"候选轮廓包含 {len(recommended_undercuts)} 处轴向半径反转，标准纵向车刀可能无法连续到达。"
    profile_waiting = any(profile.review_state == "review" for profile in rotational.profiles)
    unresolved_trial_blocker = bool(
        trial.get("status") == "blocked"
        and trial.get("reason") not in {None, "profile_review_required"}
    )
    needs_review = profile_waiting or unresolved_trial_blocker
    has_provisional = any(
        profile.review_state == "ai_provisional" for profile in rotational.profiles
    )
    return {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "status": (
            "waiting_human" if needs_review
            else "ready_for_draft" if has_provisional
            else "ready"
        ),
        "title": "确认回转轮廓并选择修正策略" if needs_review else "回转轮廓已确认",
        "summary": (
            "智能体已保留通过的前序工序，只对阻断区域等待判断。确认后将重新编译并连续仿真。"
            if needs_review else "当前回转轮廓已经人工确认，可用于下一轮 DRAFT 验证。"
        ),
        "recommended_profile_id": recommended.id if recommended else None,
        "blocker": blocker,
        "failed_operations": [str(item.get("operation_id")) for item in failed],
        "passed_operation_count": sum(item.get("status") == "passed" for item in records),
        "profiles": profiles,
        "repair_candidates": candidates,
        "next_action": (
            trial.get("next_action")
            if unresolved_trial_blocker
            else "inspect_or_provisionally_decide_profile" if profile_waiting
            else "initialize_process_draft" if has_provisional and job.plan is None
            else "trial_l32_operation" if has_provisional
            else "retry_validation"
        ),
        "production_ready": False,
    }


@app.get("/api/v1/jobs/{job_id}/agent/l32/review")
def get_l32_agent_review(job_id: str) -> dict[str, object]:
    return _build_l32_agent_review_context(job_id)


@app.post("/api/v1/jobs/{job_id}/agent/l32/profile-provisional-decision")
def provisionally_decide_l32_agent_profile(
    job_id: str, request: L32AIProfileDecisionRequest,
) -> dict[str, object]:
    """Authorize a bounded profile region for reversible DRAFT-only AI trials."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32" or job.analysis is None:
        raise HTTPException(status_code=409, detail="AI profile decisions require a completed L32 geometry analysis")
    directory = job_directory(job_id)
    path = directory / "rotational-features.json"
    if not path.is_file():
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    rotational = RotationalFeatureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    profile = next((item for item in rotational.profiles if item.id == request.profile_id), None)
    if profile is None:
        raise HTTPException(status_code=404, detail="Rotational profile not found")
    if profile.review_state == "accepted":
        raise HTTPException(status_code=409, detail="A human-accepted profile cannot be downgraded to an AI provisional decision")
    if profile.review_state == "excluded":
        raise HTTPException(status_code=409, detail="An excluded profile cannot be provisionally authorized")
    if profile.extraction_method != "exact_section":
        raise HTTPException(status_code=422, detail="AI provisional use requires an exact-section profile")

    profile_z_min = min(point.z for point in profile.points)
    profile_z_max = max(point.z for point in profile.points)
    tolerance = 1e-6
    if request.z_min_mm < profile_z_min - tolerance or request.z_max_mm > profile_z_max + tolerance:
        raise HTTPException(status_code=422, detail="AI provisional scope lies outside the extracted profile")
    if request.scope == "full" and (
        abs(request.z_min_mm - profile_z_min) > tolerance
        or abs(request.z_max_mm - profile_z_max) > tolerance
    ):
        raise HTTPException(status_code=422, detail="Full AI provisional scope must match the complete extracted profile")
    try:
        scoped = clip_rotational_profile(profile, request.z_min_mm, request.z_max_mm)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    radii = [point.radius for point in scoped.points]
    decision = AIProvisionalProfileDecision(
        scope=request.scope,
        z_min_mm=min(point.z for point in scoped.points),
        z_max_mm=max(point.z for point in scoped.points),
        diameter_min_mm=2 * min(radii),
        diameter_max_mm=2 * max(radii),
        confidence=request.confidence,
        rationale=request.rationale,
        evidence_refs=list(dict.fromkeys(request.evidence_refs)),
    )
    profile.review_state = "ai_provisional"
    profile.provisional_decision = decision
    reason = "AI bounded this exact-section profile for reversible DRAFT-only trials"
    if reason not in profile.review_reasons:
        profile.review_reasons.append(reason)
    axis = next((item for item in rotational.axes if item.id == profile.axis_id), None)
    if axis is not None and axis.review_state != "accepted":
        axis.review_state = "ai_provisional"
        if reason not in axis.review_reasons:
            axis.review_reasons.append(reason)

    job.analysis.rotational_profile_reviews[profile.id] = "ai_provisional"
    job.analysis.rotational_profile_decisions[profile.id] = decision.model_dump(mode="json")
    write_json(directory / "analysis.json", job.analysis.model_dump(mode="json"))
    write_json(path, rotational.model_dump(mode="json"))
    write_json(
        directory / "agent-l32-profile-decisions.json",
        {
            "schema_version": "1.0.0", "job_id": job_id,
            "decisions": job.analysis.rotational_profile_decisions,
            "release_status": "DRAFT", "production_ready": False,
        },
    )
    save_job(directory, job)
    invalidate_cam_artifacts(directory)
    publish_job_event(
        job_id, "l32_profile_ai_provisional",
        f"AI provisionally authorized bounded profile {profile.id} for DRAFT trials",
        42, agent_node="ai_profile_review", agent_title="AI bounded profile decision",
        agent_kind="ai_review", agent_status="completed",
        evidence=[
            {"label": "profile", "value": profile.id},
            {"label": "scope", "value": request.scope},
            {"label": "Z", "value": [decision.z_min_mm, decision.z_max_mm]},
        ],
        viewer={"kind": "features", "feature_ids": [profile.id]},
    )
    context = _build_l32_agent_review_context(job_id)
    context["decision_status"] = "ai_provisional"
    context["authorized_profile_id"] = profile.id
    context["authorized_scope"] = decision.model_dump(mode="json")
    context["next_action"] = "initialize_process_draft" if job.plan is None else "trial_l32_operation"
    return context


@app.post("/api/v1/jobs/{job_id}/agent/l32/profile-decision")
def decide_l32_agent_profile(
    job_id: str, request: L32ProfileDecisionRequest,
) -> dict[str, object]:
    with L32_PROFILE_REVIEW_LOCK:
        if job_id in L32_PROFILE_REVIEWING_JOBS:
            raise HTTPException(status_code=409, detail="该任务正在重新编译与验证")
        L32_PROFILE_REVIEWING_JOBS.add(job_id)
    try:
        review_job_rotational_profile(
            job_id, request.profile_id,
            FeatureReviewRequest(review_state=request.review_state),
        )
        directory = job_directory(job_id)
        from .agent.world_model import EvidenceReference, load_world_model
        world_path = directory / "agent-world-model.json"
        world = load_world_model(world_path)
        if world is not None:
            evidence_id = f"human-profile-review:{request.profile_id}:{world.revision + 1}"
            world.evidence.append(EvidenceReference(
                id=evidence_id, kind="human", source=f"rotational-features.json#{request.profile_id}",
                summary=f"人工将回转轮廓 {request.profile_id} 标记为 {request.review_state}",
            ))
            for question in world.open_questions:
                if question.status == "open" and (
                    "轮廓" in question.question or "倒扣" in question.question
                    or question.id.startswith("l32-profile")
                ):
                    question.status = "resolved"
                    question.answer = f"{request.profile_id}: {request.review_state}"
                    question.evidence_ids.append(evidence_id)
            world.revision += 1
            world.updated_at = utc_now()
            world.decisions.append({
                "at": utc_now(), "kind": "human_profile_review",
                "profile_id": request.profile_id, "review_state": request.review_state,
                "evidence_id": evidence_id,
            })
            world.lifecycle = "validating" if request.review_state == "accepted" else "waiting_human"
            world.next_action = "compile" if request.review_state == "accepted" else "human_review"
            world.current_objective = (
                "按人工确认轮廓重新编译并连续仿真"
                if request.review_state == "accepted" else "选择其他有效轮廓或补充制造策略"
            )
            write_json(world_path, world.model_dump(mode="json"))
        publish_job_event(
            job_id, "l32_profile_review",
            f"已{('确认' if request.review_state == 'accepted' else '排除')}回转轮廓 {request.profile_id}",
            98.9, agent_node="human_profile_review", agent_title="人工几何确认",
            agent_kind="human", agent_status="completed",
            evidence=[{"label": "轮廓", "value": request.profile_id}, {"label": "结论", "value": request.review_state}],
            viewer={"kind": "features", "feature_ids": [request.profile_id]},
        )
        result_status = "reviewed"
        validation_error = ""
        if request.review_state == "accepted" and request.retry_validation:
            (directory / "agent-l32-incremental-trial.json").unlink(missing_ok=True)
            publish_job_event(
                job_id, "l32_operation_execution", "正在按已确认轮廓重新编译并连续仿真", 99,
                agent_node="retry_after_profile_review", agent_title="重新验证 L32 工艺",
                agent_kind="tool_call", agent_status="running",
            )
            try:
                _create_l32_whole_program_with_agent_loop(job_id)
                result_status = "validated"
                publish_job_event(
                    job_id, "l32_operation_execution", "已确认轮廓的整件编译与连续仿真通过", 100,
                    agent_node="retry_after_profile_review", agent_title="L32 重新验证完成",
                    agent_kind="tool_result", agent_status="completed",
                )
            except (OSError, ValueError, HTTPException) as error:
                validation_error = str(error.detail if isinstance(error, HTTPException) else error)
                result_status = "waiting_human"
                from .agent.world_model import OpenQuestion, load_world_model as reload_world_model
                blocked_world = reload_world_model(world_path)
                if blocked_world is not None:
                    blocked_world.revision += 1
                    blocked_world.updated_at = utc_now()
                    blocked_world.lifecycle = "waiting_human"
                    blocked_world.next_action = "human_review"
                    blocked_world.current_objective = "为阻断区域选择专用工艺并重新验证"
                    if not any(
                        item.id == "l32-special-process" and item.status == "open"
                        for item in blocked_world.open_questions
                    ):
                        blocked_world.open_questions.append(OpenQuestion(
                            id="l32-special-process",
                            question="阻断区域应拆分加工，还是改用切槽刀、成形刀或动力刀具？",
                            reason=validation_error[:500],
                            priority="critical",
                            blocking=True,
                        ))
                    blocked_world.decisions.append({
                        "at": utc_now(), "kind": "validation_blocked_after_profile_review",
                        "profile_id": request.profile_id, "summary": validation_error[:500],
                    })
                    write_json(world_path, blocked_world.model_dump(mode="json"))
                publish_job_event(
                    job_id, "l32_operation_execution", "重新验证仍被真实制造约束阻断", 99,
                    agent_node="retry_after_profile_review", agent_title="需要选择专用工艺",
                    agent_kind="error", agent_status="waiting",
                    evidence=[{"label": "阻断原因", "value": validation_error[:500]}],
                )
        context = _build_l32_agent_review_context(job_id)
        context["decision_status"] = result_status
        context["validation_error"] = validation_error
        return context
    finally:
        with L32_PROFILE_REVIEW_LOCK:
            L32_PROFILE_REVIEWING_JOBS.discard(job_id)


@app.get("/api/v1/jobs/{job_id}/agent/workspace")
def get_job_agent_workspace(job_id: str) -> dict[str, object]:
    job = load_job(job_id)
    directory = job_directory(job_id)
    world_path = directory / "agent-world-model.json"
    if not world_path.is_file() and job.analysis and job.plan:
        from .agent.config import load_agent_settings as load_workspace_agent_settings
        from .agent.orchestrator import OrchestratorTools, run_manufacturing_orchestrator
        from .agent.world_model import create_manufacturing_world_model
        world = create_manufacturing_world_model(
            job_id=job_id,
            analysis=job.analysis,
            plan=job.plan,
            material=job.material,
            machine=job.machine,
            filename=job.filename,
            model_url=job.model_url,
            require_initial_perception=True,
        )
        workspace_agent_settings = load_workspace_agent_settings()
        if workspace_agent_settings.enabled:
            result = run_manufacturing_orchestrator(
                world=world,
                settings=workspace_agent_settings,
                tools=OrchestratorTools(),
                max_steps=8,
            )
            world = type(world).model_validate(result["world"])
            write_json(directory / "agent-orchestrator.json", {
                "schema_version": "1.0.0",
                "job_id": job_id,
                "status": result.get("status"),
                "next_action": result.get("action"),
                "trace": result.get("trace", []),
            })
        write_json(world_path, world.model_dump(mode="json"))
    with JOB_EVENT_CONDITION:
        events = list(JOB_EVENT_LOGS[job_id]) if job_id in JOB_EVENT_LOGS else load_job_events(job_id)
    return build_agent_workspace(
        job_id=job_id,
        job_status=job.status,
        directory=directory,
        events=events,
    )


@app.post("/api/v1/jobs/{job_id}/agent/model-view")
def render_job_agent_model_view(job_id: str) -> dict[str, object]:
    """Render deterministic model views without running an AI review."""
    job = load_job(job_id)
    if job.status != "completed" or not job.analysis:
        raise HTTPException(status_code=409, detail="需要已完成的几何分析")
    directory = job_directory(job_id)
    model_path = directory / "model.stl"
    if not model_path.is_file():
        raise HTTPException(status_code=409, detail="任务缺少可渲染的 STL 模型")
    from .agent.perception import render_model_evidence
    try:
        views = render_model_evidence(model_path, directory)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "job_id": job_id,
        "source": "model.stl",
        "generated_by": "deterministic_stl_renderer",
        "views": {
            name: f"/api/v1/jobs/{job_id}/files/{path.name}"
            for name, path in views.items()
        },
    }


@app.post("/api/v1/jobs/{job_id}/agent/perceive")
def perceive_job_model(job_id: str) -> dict[str, object]:
    """Run one evidence-backed perception question, then persist the graph state."""
    job = load_job(job_id)
    if job.status != "completed" or not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="A completed geometry analysis and plan are required")
    directory = job_directory(job_id)
    from .agent.world_model import ManufacturingWorldModel, OpenQuestion, load_world_model
    world_path = directory / "agent-world-model.json"
    world = load_world_model(world_path)
    if world is None:
        get_job_agent_workspace(job_id)
        world = load_world_model(world_path)
    if world is None:
        raise HTTPException(status_code=409, detail="Manufacturing world model is unavailable")
    if not any(item.status == "open" and item.blocking for item in world.open_questions):
        if not world.current_operation_id:
            raise HTTPException(status_code=409, detail="No operation needs perception")
        operation = next((item for item in world.operations if item.id == world.current_operation_id), None)
        world.open_questions.append(OpenQuestion(
            id=f"operation:{world.current_operation_id}:perception:{world.revision}",
            question=f"当前模型几何与装夹证据是否支持 {world.current_operation_id} {operation.name if operation else ''}？",
            reason="工序执行前按需核对几何与可达性",
            priority="high",
            blocking=True,
        ))
        world.next_action = "perceive"
    with AGENT_PERCEPTION_LOCK:
        if job_id in AGENT_PERCEIVING_JOBS:
            raise HTTPException(status_code=409, detail="Multimodal perception is already running for this job")
        AGENT_PERCEIVING_JOBS.add(job_id)
    try:
        from .agent.config import load_agent_settings
        from .agent.orchestrator import OrchestratorTools, run_manufacturing_orchestrator
        from .agent.perception import build_perception_tool
        settings = load_agent_settings()
        if not settings.enabled:
            raise HTTPException(status_code=409, detail="Manufacturing agent is disabled")
        if not qwen_config_payload().get("configured"):
            raise HTTPException(status_code=503, detail="Qwen API key is not configured")
        result = run_manufacturing_orchestrator(
            world=world,
            settings=settings,
            tools=OrchestratorTools(perceive=build_perception_tool(directory)),
            max_steps=8,
        )
        updated = ManufacturingWorldModel.model_validate(result["world"])
        if result.get("status") == "blocked" and result.get("error"):
            raise HTTPException(status_code=502, detail=str(result["error"]))
        write_json(world_path, updated.model_dump(mode="json"))
        write_json(directory / "agent-orchestrator.json", {
            "schema_version": "1.0.0", "job_id": job_id,
            "status": result.get("status"), "next_action": updated.next_action,
            "trace": result.get("trace", []),
        })
        publish_job_event(
            job_id, "orchestrator_perceive",
            "多模态模型观察完成" if not any(q.status == "open" and q.blocking for q in updated.open_questions)
            else "多模态模型观察完成，仍有待确认问题",
            100,
            agent_kind="tool_result", agent_status="completed",
            agent_title="按需几何感知",
            evidence=[{"label": "证据图", "value": "agent-perception-contact-sheet.png"}],
        )
        return build_agent_workspace(
            job_id=job_id, job_status=job.status, directory=directory,
            events=load_job_events(job_id),
        )
    finally:
        with AGENT_PERCEPTION_LOCK:
            AGENT_PERCEIVING_JOBS.discard(job_id)


@app.post("/api/v1/jobs/{job_id}/agent/trial")
def trial_first_operation(job_id: str) -> dict[str, object]:
    """Run the first operation in isolation; never publish its NC as production code."""
    job = load_job(job_id)
    if job.status != "completed" or not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="需要已完成的模型分析与工艺方案")
    if job.plan.process_kind != "subtractive" or job.device_id == "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="当前试跑仅支持 FreeCAD 减材工序，L32 请使用专用车削工作台")
    if job.plan.automation_status == "unsupported":
        raise HTTPException(status_code=409, detail="工艺方案已被判定超出自动 CAM 能力，不能试跑")
    first = next((operation for setup in job.plan.setups for operation in setup.operations if operation.enabled), None)
    if first is None:
        raise HTTPException(status_code=409, detail="没有可试跑的工序")
    if not resolve_executable(FREECAD_CMD):
        raise HTTPException(status_code=503, detail="FreeCAD CAM 运行环境不可用")
    from .agent.config import load_agent_settings
    from .agent.world_model import EvidenceReference, load_world_model, utc_now as world_utc_now
    from .agent.operation_trial import run_operation_trial

    if not load_agent_settings().enabled:
        raise HTTPException(status_code=409, detail="制造智能体未启用")
    directory = job_directory(job_id)
    world_path = directory / "agent-world-model.json"
    world = load_world_model(world_path)
    if world is None:
        get_job_agent_workspace(job_id)
        world = load_world_model(world_path)
    questions = [item.question for item in world.open_questions if item.status == "open" and item.blocking] if world else []
    with AGENT_TRIAL_LOCK:
        if job_id in AGENT_TRIALING_JOBS:
            raise HTTPException(status_code=409, detail="当前任务已有工序试跑在执行")
        AGENT_TRIALING_JOBS.add(job_id)
    publish_job_event(job_id, "operation_trial", f"正在独立生成 {first.id} 的刀路与余料仿真", 5,
                      agent_kind="tool_call", agent_status="running", agent_title="首道工序试跑")
    try:
        result = run_operation_trial(
            directory=directory, source_filename=job.filename, analysis=job.analysis,
            plan=job.plan, operation_id=first.id, freecad_command=FREECAD_CMD,
            adapter_script=CAM_ADAPTER_SCRIPT, open_questions=questions,
        )
        result["files"] = {name: url.replace("{job_id}", job_id) for name, url in result["files"].items()}
        write_json(directory / "agent-operation-trial.json", result)
        write_json(directory / "agent-trials" / result["trial_id"] / "result.json", result)
        if world is not None:
            trial_evidence_id = f"trial:{result['trial_id']}"
            world.evidence.append(EvidenceReference(
                id=trial_evidence_id, kind="simulation", source="agent-operation-trial.json",
                summary=f"{first.id} 独立刀路及高度场试跑：{result['status']}（非生产验证）",
                operation_id=first.id,
            ))
            operation = next((item for item in world.operations if item.id == first.id), None)
            if operation is not None:
                operation.evidence_ids.append(trial_evidence_id)
            world.decisions.append({
                "at": world_utc_now(), "kind": "single_operation_trial",
                "operation_id": first.id, "trial_id": result["trial_id"],
                "status": result["status"], "production_ready": False,
            })
            world.revision += 1
            world.updated_at = world_utc_now()
            write_json(world_path, world.model_dump(mode="json"))
        publish_job_event(
            job_id, "operation_trial", f"{first.id} 独立试跑完成：{result['status']}", 100,
            agent_kind="validation", agent_status="failed" if result["status"] == "blocked" else "completed",
            agent_title="首道工序试跑",
            evidence=[
                {"label": "状态", "value": result["status"]},
                {"label": "切削段", "value": result["evidence"]["cut_segment_count"]},
                {"label": "未决装夹问题", "value": len(questions)},
            ],
            artifacts=[{"id": "agent-operation-trial", "label": "首道工序试跑证据", "kind": "json",
                        "url": f"/api/v1/jobs/{job_id}/files/agent-operation-trial.json"}],
        )
        return result
    except (RuntimeError, ValueError, OSError) as error:
        publish_job_event(job_id, "operation_trial", str(error), 100,
                          agent_kind="error", agent_status="failed", agent_title="首道工序试跑")
        raise HTTPException(status_code=502, detail=str(error)) from error
    finally:
        with AGENT_TRIAL_LOCK:
            AGENT_TRIALING_JOBS.discard(job_id)


@app.get("/api/v1/jobs/{job_id}/agent/trials/{trial_id}/{filename}")
def get_operation_trial_file(job_id: str, trial_id: str, filename: str) -> FileResponse:
    load_job(job_id)
    if not trial_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in trial_id):
        raise HTTPException(status_code=404, detail="试跑文件不存在")
    if filename not in {"result.json", "plan.json", "toolpath.json", "verification.json", "simulation.json", "collision.json", "remaining-stock.png"}:
        raise HTTPException(status_code=404, detail="试跑文件不存在；试跑 NC 不对外发布")
    path = job_directory(job_id) / "agent-trials" / trial_id / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="试跑文件不存在")
    return FileResponse(path)


@app.get("/api/v1/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, response: Response) -> JobResponse:
    # A process plan may be rebuilt while its job URL remains unchanged. Do
    # not let a browser reuse an older operation list for the same job.
    response.headers["Cache-Control"] = "no-store"
    job = load_job(job_id)
    if job.device_id == "citizen-cincom-l32" and job.analysis and job.plan:
        # Stored plans predate newer geometry targets; refresh the read-only
        # coverage response without rewriting the user's operations or binding.
        job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    return job


@app.post("/api/v1/jobs/{job_id}/agent/plan/initialize", response_model=JobResponse)
def initialize_harness_process_draft(job_id: str) -> JobResponse:
    """Create stock/setup scaffolding only; Harness owns every operation decision."""
    job = load_job(job_id)
    if job.status != "completed" or job.analysis is None:
        raise HTTPException(status_code=409, detail="几何分析尚未完成")
    if job.plan is not None:
        owner = (job.plan.ai_planning or {}).get("planning_owner")
        if owner == "deepseek_harness":
            return job
        raise HTTPException(status_code=409, detail="任务已经存在非 Harness 工艺方案，不能覆盖")

    plan = build_process_plan(job.analysis, material=job.material, machine=job.machine)
    for setup in plan.setups:
        setup.operations = []
    plan.title = f"Harness 自主工艺草案 · {job.filename}"
    plan.estimated_minutes = 0
    plan.automation_status = "review"
    plan.blocking_reasons = ["Harness 尚未逐道建立并验证工序"]
    plan.warnings = [
        "该方案由 DeepSeek Harness 从空白工序序列开始构建，当前仅包含确定性毛坯与装夹脚手架。",
    ]
    plan.assumptions = [
        *plan.assumptions,
        "装夹脚手架来自确定性几何与设备约束，具体工序由 Harness 逐道决策。",
    ]
    plan.ai_planning = {
        "planning_owner": "deepseek_harness",
        "planning_mode": "operation_by_operation",
        "baseline_operations_imported": False,
        "release_status": "DRAFT",
    }
    plan.coverage = evaluate_plan_coverage(job.analysis, plan)
    plan.manufacturing_route = build_manufacturing_route(job.analysis, plan)
    plan.knowledge_assessment = assess_plan_knowledge(job.analysis, plan)
    job.plan = plan
    directory = job_directory(job_id)
    provision_default_l32_planning_instance(job, directory)
    save_job(directory, job)
    write_json(directory / "plan.json", plan.model_dump(mode="json"))
    publish_job_event(
        job_id, "harness_plan", "Harness 已建立空白工艺草案，等待逐道规划", 40,
        agent_node="harness_initialize_plan", agent_title="建立工艺决策工作区",
        agent_kind="decision", agent_status="completed",
        evidence=[
            {"label": "装夹数", "value": len(plan.setups)},
            {"label": "初始工序数", "value": 0},
            {"label": "规划所有者", "value": "DeepSeek Harness"},
        ],
        viewer={"kind": "model", "url": job.model_url},
    )
    return job


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


@app.delete("/api/v1/jobs/{job_id}")
def delete_job(job_id: str) -> dict[str, object]:
    """Permanently delete one completed/failed job and all of its stored artifacts."""
    directory = job_directory(job_id)
    job = load_job(job_id)
    if job.status == "processing":
        raise HTTPException(status_code=409, detail="任务仍在处理中，暂时不能删除")
    if job_id in CAM_STREAMING_JOBS or job_id in AI_REVIEWING_JOBS:
        raise HTTPException(status_code=409, detail="任务正在生成或审查中，暂时不能删除")
    shutil.rmtree(directory)
    with JOB_EVENT_CONDITION:
        JOB_EVENT_LOGS.pop(job_id, None)
    return {"deleted": True, "job_id": job_id}


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
    if request.review_state == "accepted":
        axis = next((item for item in analysis.axes if item.id == profile.axis_id), None)
        if axis is not None:
            axis.review_state = "accepted"
            axis.review_reasons = []
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
            elif (
                previous.workpiece_side == "back"
                and previous.enabled
                and job.plan.stock.get("nonrotational_turning_limit_z_mm") is None
            ):
                operation.enabled = True
    reapply_bound_l32_machine_configuration(job, directory)
    write_json(directory / "analysis.json", job.analysis.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    result = persist_rotational_analysis(directory, job, job.analysis)
    assert result is not None
    save_job(directory, job)
    invalidate_cam_artifacts(directory)
    return result


@app.patch(
    "/api/v1/jobs/{job_id}/turning/features/{feature_id}",
    response_model=RotationalFeatureAnalysis,
)
def review_job_rotational_feature(
    job_id: str, feature_id: str, request: FeatureReviewRequest,
) -> RotationalFeatureAnalysis:
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Rotational feature review requires an L32 job")
    directory = job_directory(job_id)
    path = directory / "rotational-features.json"
    if not path.is_file():
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    try:
        analysis = RotationalFeatureAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Stored rotational analysis is invalid") from error
    feature = next((item for item in analysis.features if item.id == feature_id), None)
    if feature is None:
        raise HTTPException(status_code=404, detail="Rotational feature not found")
    feature.review_state = request.review_state
    if request.review_state == "accepted":
        feature.confidence = max(feature.confidence, 0.9)
        reason = "制造工程师已人工确认该回转子特征"
        if reason not in feature.review_reasons:
            feature.review_reasons.append(reason)
    elif request.review_state == "excluded":
        reason = "制造工程师已从自动工艺规划中排除该回转子特征"
        if reason not in feature.review_reasons:
            feature.review_reasons.append(reason)
    write_json(path, analysis.model_dump(mode="json"))
    invalidate_cam_artifacts(directory)
    return analysis


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
    reapply_bound_l32_machine_configuration(job, directory)
    invalidate_cam_artifacts(directory)
    write_json(directory / "manufacturing-requirements.json", requirements.model_dump(mode="json"))
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    persist_rotational_analysis(directory, job, job.analysis)
    save_job(directory, job)
    return job


@app.post(
    "/api/v1/jobs/{job_id}/turning/groove-bindings/confirm",
    response_model=JobResponse,
)
def confirm_job_groove_binding(
    job_id: str, request: GrooveBindingConfirmRequest,
) -> JobResponse:
    """Bind reviewed drawing dimensions to one unique STEP groove candidate."""

    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Groove binding confirmation requires an L32 job")
    if not job.analysis or not job.plan:
        raise HTTPException(status_code=409, detail="Geometry analysis and process plan are required")

    rotational = infer_rotational_features(job.analysis)
    feature = next(
        (
            item for item in rotational.features
            if item.id == request.groove_feature_id
            and item.kind in {"external_groove_candidate", "internal_groove_candidate"}
        ),
        None,
    )
    if feature is None:
        raise HTTPException(status_code=404, detail="STEP groove candidate not found")
    side = "internal" if feature.kind == "internal_groove_candidate" else "external"
    if request.requirement_kind == "external_groove" and side != "external":
        raise HTTPException(status_code=422, detail="External drawing groove cannot bind an internal STEP candidate")
    if request.requirement_kind == "internal_groove" and side != "internal":
        raise HTTPException(status_code=422, detail="Internal drawing groove cannot bind an external STEP candidate")
    profile = next((item for item in rotational.profiles if item.id == feature.profile_id), None)
    if profile is None or profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise HTTPException(status_code=409, detail="Groove binding requires an accepted exact rotational profile")

    drawing_requirement = DrawingGrooveRequirement(
        id=request.requirement_id,
        kind=request.requirement_kind,
        side=side,
        width_mm=request.confirmed_groove_width_mm,
        depth_mm=request.confirmed_groove_depth_mm,
        bottom_diameter_mm=request.confirmed_bottom_diameter_mm,
        verification_status="verified_dimensions",
        raw_text=request.raw_text,
        source={"method": "engineer_structured_drawing_confirmation"},
    )
    binding = bind_groove_requirement(
        drawing_requirement, groove_candidate_evidence(rotational.features),
    )
    if binding.status != "matched":
        raise HTTPException(status_code=422, detail=binding.reason)
    if binding.matched_candidate_id != feature.id:
        raise HTTPException(
            status_code=422,
            detail=f"Reviewed dimensions uniquely match {binding.matched_candidate_id}, not the selected candidate",
        )

    requirements = (
        job.plan.manufacturing_requirements.model_copy(deep=True)
        if job.plan.manufacturing_requirements is not None
        else ManufacturingRequirements(
            source_system="engineer-review",
            status="incomplete",
        )
    )
    existing = next(
        (item for item in requirements.requirements if item.id == request.requirement_id), None,
    )
    if existing is not None and existing.type not in {
        "external_groove", "internal_groove", "seal_groove",
    }:
        raise HTTPException(status_code=409, detail="Requirement id is already used by another requirement type")
    requirement = ManufacturingRequirement(
        id=request.requirement_id,
        type=request.requirement_kind,
        subtype=side,
        nominal=request.confirmed_bottom_diameter_mm,
        unit="mm",
        cad_feature_ids=[feature.id],
        mapping_status="matched",
        verification_status="verified_engineer",
        confidence=min(feature.confidence, 0.95),
        raw_text=request.raw_text,
        source={
            "binding_method": "cnc_groove_dimensions_engineer_confirmation",
            "confirmed_groove_feature_id": feature.id,
            "confirmed_groove_width_mm": request.confirmed_groove_width_mm,
            "confirmed_groove_depth_mm": request.confirmed_groove_depth_mm,
            "confirmed_bottom_diameter_mm": request.confirmed_bottom_diameter_mm,
            "confirmed_side": side,
            "reviewer": request.reviewer,
            "confirmed_at": utc_now(),
        },
    )
    if existing is None:
        requirements.requirements.append(requirement)
    else:
        requirements.requirements[requirements.requirements.index(existing)] = requirement
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
    for setup in job.plan.setups:
        for index, operation in enumerate(setup.operations):
            previous = previous_operations.get(operation.id)
            if (
                previous is not None
                and previous.type == operation.type
                and previous.parameters.get("engineering_review_status") == "verified_engineer"
            ):
                setup.operations[index] = previous

    directory = job_directory(job_id)
    reapply_bound_l32_machine_configuration(job, directory)
    invalidate_cam_artifacts(directory)
    write_json(directory / "manufacturing-requirements.json", requirements.model_dump(mode="json"))
    write_json(directory / "groove-binding.json", {
        "schema_version": "1.0.0",
        "requirement": requirement.model_dump(mode="json"),
        "binding": binding.model_dump(mode="json"),
    })
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
    reapply_bound_l32_machine_configuration(job, directory)
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
    "/api/v1/jobs/{job_id}/turning/grooving-operations/{operation_id}/review",
    response_model=JobResponse,
)
def review_job_grooving_operation(
    job_id: str, operation_id: str, request: GroovingOperationReviewRequest,
) -> JobResponse:
    """Approve an automatically detected external or internal groove for DRAFT simulation."""
    job = load_job(job_id)
    if job.device_id != "citizen-cincom-l32":
        raise HTTPException(status_code=409, detail="Grooving review requires an L32 job")
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
        raise HTTPException(status_code=404, detail="Grooving operation not found")
    if operation.type != "turn_grooving" or operation.source != "automatic":
        raise HTTPException(status_code=422, detail="Only an automatically planned groove can use this review")
    rotational = persist_rotational_analysis(job_directory(job_id), job, job.analysis)
    if rotational is None:
        raise HTTPException(status_code=409, detail="Rotational analysis is not available")
    groove_side = str(operation.parameters.get("groove_side", "external"))
    expected_feature_kind = (
        "internal_groove_candidate" if groove_side == "internal" else "external_groove_candidate"
    )
    expected_profile_side = "inner" if groove_side == "internal" else "outer"
    feature = next(
        (
            item for item in rotational.features
            if item.id in operation.feature_ids and item.kind == expected_feature_kind
        ),
        None,
    )
    profile = next(
        (
            item for item in rotational.profiles
            if item.id in operation.feature_ids and item.side == expected_profile_side
        ),
        None,
    )
    if feature is None or profile is None:
        raise HTTPException(status_code=409, detail="Groove feature traceability is incomplete")
    if profile.extraction_method != "exact_section" or profile.review_state != "accepted":
        raise HTTPException(status_code=409, detail="Grooving review requires an accepted exact profile")
    if not request.profile_form_confirmed:
        raise HTTPException(status_code=422, detail="Groove profile form must be confirmed from the drawing/CAD")
    if groove_side == "internal":
        finish = next(
            (
                item for setup in job.plan.setups for item in setup.operations
                if item.type == "turn_id_finishing" and profile.id in item.feature_ids
            ),
            None,
        )
        if (
            finish is None
            or not finish.enabled
            or finish.parameters.get("engineering_review_status") != "verified_engineer"
        ):
            raise HTTPException(status_code=409, detail="Review and enable the base-bore finishing operation before internal grooving")
    requirement_id = str(operation.parameters.get("drawing_requirement_id") or "")
    verified_groove_requirements = [
        item for item in (job.plan.manufacturing_requirements.requirements if job.plan.manufacturing_requirements else [])
        if item.id == requirement_id
        and item.mapping_status == "matched"
        and item.verification_status == "verified_engineer"
        and item.cad_feature_ids == [feature.id]
    ]
    if (
        operation.parameters.get("drawing_binding_status") != "matched"
        or len(verified_groove_requirements) != 1
    ):
        raise HTTPException(
            status_code=409,
            detail="Confirm a unique engineer-verified drawing-to-STEP groove binding before operation review",
        )
    expected_width = float(feature.width_mm)
    expected_diameter = (
        max(feature.radius_start, feature.radius_end) * 2
        if groove_side == "internal"
        else min(feature.radius_start, feature.radius_end) * 2
    )
    if abs(request.confirmed_groove_width_mm - expected_width) > 0.05:
        raise HTTPException(status_code=422, detail="Confirmed groove width does not match the exact profile")
    if abs(request.confirmed_final_diameter_mm - expected_diameter) > 0.1:
        raise HTTPException(status_code=422, detail="Confirmed groove bottom diameter does not match the exact profile")
    if request.peck_depth_mm > feature.depth_mm + 1e-9:
        raise HTTPException(status_code=422, detail="Grooving peck depth cannot exceed the groove radial depth")
    try:
        tool = get_tool(request.groove_tool_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    expected_tool_kind = "internal_grooving" if groove_side == "internal" else "grooving"
    if tool.kind != expected_tool_kind or tool.cutting_width_mm is None:
        raise HTTPException(status_code=422, detail=f"Reviewed tool must be a {expected_tool_kind} tool with a cutting width")
    if tool.cutting_width_mm > request.confirmed_groove_width_mm + 1e-9:
        raise HTTPException(status_code=422, detail="Grooving tool is wider than the confirmed groove")

    reviewed_operation = operation.model_copy(deep=True)
    reviewed_operation.tool = tool
    if groove_side == "internal":
        if request.confirmed_stickout_mm is None:
            raise HTTPException(status_code=422, detail="Internal grooving requires confirmed tool stickout")
        reviewed_operation.tool.stickout_mm = request.confirmed_stickout_mm
    reviewed_operation.parameters.update({
        "groove_width_mm": request.confirmed_groove_width_mm,
        "final_diameter_mm": request.confirmed_final_diameter_mm,
        "peck_depth_mm": request.peck_depth_mm,
    })
    stock_radius = float(job.plan.stock.get("diameter_mm", 0) or 0) / 2
    initial_bore_radius = min(point.radius for point in profile.points) if groove_side == "internal" else 0
    reachability = assess_turning_reachability(
        reviewed_operation, profile,
        TurningContext(
            machine_snapshot_hash=job.machine_configuration_hash or "0" * 64,
            stock_radius_mm=stock_radius,
            initial_bore_radius_mm=initial_bore_radius,
        ),
        assembly_clearance_mm=request.assembly_clearance_mm,
    )
    if reachability.status == "failed":
        raise HTTPException(
            status_code=422,
            detail="Grooving reachability failed: " + "; ".join(reachability.blocking_reasons),
        )
    operation.tool = reviewed_operation.tool
    operation.parameters.update({
        **reviewed_operation.parameters,
        "groove_tool_inventory_id": request.groove_tool_inventory_id,
        "confirmed_stickout_mm": request.confirmed_stickout_mm or tool.stickout_mm,
        "assembly_clearance_mm": request.assembly_clearance_mm,
        "profile_form_confirmed": True,
        "engineering_review_status": "verified_engineer",
        "engineering_reviewer": request.reviewer,
        "engineering_reviewed_at": utc_now(),
    })
    operation.enabled = True
    operation.status = "warning"
    operation.generation_state = "dirty"
    review_reason = "槽宽、槽底直径、槽形、分层切入和切槽刀实物已完成 DRAFT 级工程审核"
    if review_reason not in operation.rationale:
        operation.rationale.append(review_reason)
    job.plan.coverage = evaluate_plan_coverage(job.analysis, job.plan)
    job.plan.manufacturing_route = build_manufacturing_route(job.analysis, job.plan)
    job.plan.knowledge_assessment = assess_plan_knowledge(job.analysis, job.plan)
    directory = job_directory(job_id)
    invalidate_cam_artifacts(directory)
    write_json(directory / "grooving-review.json", {
        "schema_version": "1.0.0",
        "operation_id": operation.id,
        "feature_id": feature.id,
        "profile_id": profile.id,
        "tool_id": tool.id,
        "tool_inventory_id": request.groove_tool_inventory_id,
        "groove_width_mm": request.confirmed_groove_width_mm,
        "final_diameter_mm": request.confirmed_final_diameter_mm,
        "peck_depth_mm": request.peck_depth_mm,
        "groove_side": groove_side,
        "confirmed_stickout_mm": request.confirmed_stickout_mm,
        "assembly_clearance_mm": request.assembly_clearance_mm,
        "reviewer": request.reviewer,
        "reachability": reachability.model_dump(mode="json"),
    })
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
    stage_index = int(operation.parameters.get("drilling_stage_index", 1))
    if stage_index > 1:
        preceding = [
            item for setup in job.plan.setups for item in setup.operations
            if item.type == "axial_drilling"
            and profile.id in item.feature_ids
            and int(item.parameters.get("drilling_stage_index", 1)) < stage_index
        ]
        if any(
            not item.enabled
            or item.parameters.get("engineering_review_status") != "verified_engineer"
            for item in preceding
        ):
            raise HTTPException(status_code=409, detail="Review deeper pre-bore stages before this enlarged stage")
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
    length_to_diameter_ratio = profile_depth / drill.diameter_mm
    deep_hole_required = length_to_diameter_ratio > 5.0
    if deep_hole_required and request.chip_evacuation_strategy != "deep_hole_peck":
        raise HTTPException(status_code=422, detail="Hole depth exceeds 5×D and requires the deep-hole peck strategy")
    if (
        request.chip_evacuation_strategy == "deep_hole_peck"
        and not request.through_tool_coolant_confirmed
    ):
        raise HTTPException(status_code=422, detail="Deep-hole peck drilling requires confirmed through-tool coolant")
    if (
        request.chip_evacuation_strategy == "deep_hole_peck"
        and request.peck_depth_mm > drill.diameter_mm * 0.5 + 1e-9
    ):
        raise HTTPException(status_code=422, detail="Deep-hole peck depth cannot exceed 0.5×D")
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
        "length_to_diameter_ratio": round(length_to_diameter_ratio, 3),
        "chip_evacuation_strategy": request.chip_evacuation_strategy,
        "through_tool_coolant_confirmed": request.through_tool_coolant_confirmed,
        "confirmed_stickout_mm": request.confirmed_stickout_mm,
        "drill_inventory_id": request.drill_inventory_id,
        "engineering_review_status": "verified_engineer",
        "engineering_reviewer": request.reviewer,
        "engineering_reviewed_at": utc_now(),
    })
    operation.enabled = True
    operation.status = "warning"
    operation.generation_state = "dirty"
    reason = "钻头实物、有效刃长、伸出、啄钻深度、排屑/内冷和钻尖越程已通过 DRAFT 级审核"
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
        "drilling_stage_index": stage_index,
        "planned_drilling_stage_count": int(operation.parameters.get("planned_drilling_stage_count", 1)),
        "target_bore_diameter_mm": float(operation.parameters.get("target_bore_diameter_mm", 0)),
        "drill_diameter_mm": drill.diameter_mm, "profile_depth_mm": profile_depth,
        "programmed_depth_mm": programmed_depth, "tip_length_mm": tip_length,
        "length_to_diameter_ratio": length_to_diameter_ratio,
        "deep_hole_required": deep_hole_required,
        "chip_evacuation_strategy": request.chip_evacuation_strategy,
        "through_tool_coolant_confirmed": request.through_tool_coolant_confirmed,
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

    prebores = sorted(
        (
            item for setup in job.plan.setups for item in setup.operations
            if item.type == "axial_drilling"
            and item.source == "automatic"
            and profile.id in item.feature_ids
        ),
        key=lambda item: int(item.parameters.get("drilling_stage_index", 1)),
    )
    prebore = prebores[0] if prebores else None
    if prebore is not None:
        if any(
            not item.enabled
            or item.parameters.get("engineering_review_status") != "verified_engineer"
            for item in prebores
        ):
            raise HTTPException(status_code=409, detail="Review and enable every planned pre-bore stage before approving boring")
        if abs(prebore.tool.diameter_mm - request.initial_bore_diameter_mm) > 1e-6:
            raise HTTPException(status_code=422, detail="Initial bore diameter must match the reviewed pre-bore drill")
        required_depth = float(operation.parameters.get("profile_depth_mm", 0))
        available_depth = float(prebore.parameters.get("full_diameter_depth_mm", 0))
        if available_depth + 1e-9 < required_depth:
            raise HTTPException(status_code=422, detail="Reviewed pre-bore does not cover the inner-profile depth")

    reviewed_operation = operation.model_copy(deep=True)
    sharp_shoulder_count = int(operation.parameters.get("sharp_inner_shoulder_count", 0))
    if request.finishing_tool_id is not None:
        try:
            selected_tool = get_tool(request.finishing_tool_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if operation.type != "turn_id_finishing" or selected_tool.kind != "turning_id":
            raise HTTPException(
                status_code=422,
                detail="Finishing tool override requires an inner finishing operation and turning_id tool",
            )
        reviewed_operation.tool = selected_tool
    if sharp_shoulder_count > 0 and operation.type == "turn_id_finishing":
        maximum_nose_radius = float(operation.parameters.get("maximum_finish_nose_radius_mm", 0.05))
        if request.shoulder_strategy != "small_nose_tool":
            raise HTTPException(status_code=422, detail="Sharp inner shoulder requires the small_nose_tool strategy")
        if request.finishing_tool_id is None:
            raise HTTPException(status_code=422, detail="Sharp inner shoulder requires a reviewed finishing tool")
        if (
            reviewed_operation.tool.nose_radius_mm is None
            or reviewed_operation.tool.nose_radius_mm > maximum_nose_radius + 1e-9
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Sharp inner shoulder requires nose radius <= {maximum_nose_radius:.3f} mm",
            )
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
        "finishing_tool_id": request.finishing_tool_id or operation.tool.id,
        "shoulder_strategy": request.shoulder_strategy,
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
        job.status = "completed"
        job.analysis = analysis
        job.plan = plan
        job.model_url = f"/api/v1/jobs/{job_id}/files/model.stl"
        reapply_bound_l32_machine_configuration(job, directory)
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
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
    reapply_bound_l32_machine_configuration(job, directory)
    write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
    save_job(directory, job)
    return job


@app.get("/api/v1/jobs/{job_id}/files/{filename}")
def get_job_file(job_id: str, filename: str) -> FileResponse:
    allowed = {
        "model.stl", "drawing.pdf", "analysis.json", "rotational-features.json", "plan.json",
        "manufacturing-specification.json", "manufacturing-requirements.json", "measurement-link.json",
        "planning-guidance.json", "agent-plan.json", "agent-evaluation.json", "operation-audit.json",
        "agent-execution.json", "agent-world-model.json", "agent-orchestrator.json",
        "agent-perception.json", "agent-perception-contact-sheet.png", "agent-operation-trial.json",
        "agent-view-isometric.png", "agent-view-front.png", "agent-view-right.png", "agent-view-top.png",
        "agent-remediation.json",
        "agent-l32-execution.json", "agent-l32-incremental-trial.json", "agent-l32-rolling-loop.json",
        "agent-l32-profile-decisions.json", "agent-l32-autonomous-process.json",
        "ai-plan.json", "cam.FCStd", "program.nc", "toolpath.json", "cam-manifest.json", "verification.json",
        "simulation.json", "collision.json", "remediation.json", "remediation-history.json",
        "turning-toolpath-ir.json", "turning-simulation.json", "turning-verification.json", "turning-reachability.json", "turning-draft.json",
        "turning-transfer-ir.json", "turning-transfer-draft.json",
        "turning-backside-ir.json", "turning-backside-draft.json",
        "turning-whole-program-ir.json", "turning-whole-program-draft.json",
        "turning-whole-program-timeline.json", "turning-continuous-simulation.json",
        "turning-inner-bore-chain-ir.json", "turning-inner-bore-chain-draft.json",
    }
    generated_artifact = (
        Path(filename).name == filename
        and ((filename.startswith("program-") and filename.endswith(".nc"))
             or (filename.startswith("camotics-") and filename.endswith(".stl"))
             or (filename.startswith("l32-material-") and filename.endswith(".stl"))
             or (filename.startswith("agent-perception-") and filename.endswith(".json")))
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
        ".png": "image/png",
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
    if not job.plan.coverage or not job.plan.coverage.production_ready:
        coverage = job.plan.coverage
        uncovered = (
            sum(item.state in {"uncovered", "unresolved"} for item in coverage.targets)
            if coverage else 0
        )
        review = coverage.review_count if coverage else 0
        raise HTTPException(
            status_code=409,
            detail=(
                "工艺覆盖门禁未通过，方案不可批准："
                f"未覆盖 {uncovered} 项，待复核 {review} 项，"
                f"自动化状态 {job.plan.automation_status}"
            ),
        )
    if ai_block_reason := _ai_review_block_reason(job_directory(job_id)):
        raise HTTPException(
            status_code=409,
            detail=f"AI 工艺审查阻止批准：{ai_block_reason}",
        )
    if agent_block_reason := _agent_evaluation_block_reason(job_directory(job_id)):
        raise HTTPException(
            status_code=409,
            detail=f"智能体工艺门禁阻止批准：{agent_block_reason}",
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
                *job.analysis.planar_machining_features,
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
    reapply_bound_l32_machine_configuration(job, directory)
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
    planar_machining = next((item for item in job.analysis.planar_machining_features if item.id == feature_id), None)
    if planar_machining:
        return "planar_surface"
    rotational_path = job_directory(job.id) / "rotational-features.json"
    if rotational_path.is_file():
        try:
            rotational = RotationalFeatureAnalysis.model_validate_json(
                rotational_path.read_text(encoding="utf-8"),
            )
            profile = next((item for item in rotational.profiles if item.id == feature_id), None)
            if profile is not None:
                return "inner_rotational_profile" if profile.side == "inner" else "outer_rotational_profile"
            turning_feature = next((item for item in rotational.features if item.id == feature_id), None)
            if turning_feature is not None:
                if turning_feature.kind == "external_groove_candidate":
                    return "od_groove"
                if turning_feature.kind == "internal_groove_candidate":
                    return "id_groove"
                if turning_feature.kind == "thread_form_candidate":
                    return "internal_thread" if turning_feature.thread_side == "internal" else "external_thread"
                if turning_feature.kind == "cutoff_boundary":
                    return "cutoff_plane"
                if turning_feature.kind in {"inner_bore", "inner_taper"}:
                    return "inner_rotational_profile"
                return "outer_rotational_profile"
        except (OSError, ValueError):
            pass
    # L32 accepted rotational entities are stored outside GeometryAnalysis.
    # Only IDs already referenced by the formal plan are eligible here; a
    # client cannot invent an RP/TPF identifier to bypass geometry checks.
    planned = [operation for setup in (job.plan.setups if job.plan else []) for operation in setup.operations]
    if any(feature_id in operation.feature_ids for operation in planned):
        if feature_id.startswith("RP-INNER-"):
            return "inner_rotational_profile"
        if feature_id.startswith("RP-OUTER-"):
            return "outer_rotational_profile"
        if feature_id.startswith("TPF-OUTER-"):
            return "od_groove"
        if feature_id.startswith("TPF-INNER-"):
            return "id_groove"
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


def _validate_reference_profile(job: JobResponse, profile_id: str | None) -> None:
    if profile_id is None:
        return
    if _feature_type(job, profile_id) not in {
        "outer_rotational_profile", "inner_rotational_profile",
    }:
        raise HTTPException(status_code=422, detail="reference_profile_id must reference a rotational profile")


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
    l32_agent_draft = (
        job.device_id == "citizen-cincom-l32"
        and (
            definition.engine.provider == "turning"
            or definition.id in {
                "pocket_roughing", "pocket_finishing",
                "live_tool_contour_roughing", "live_tool_contour_finishing",
            }
        )
    )
    if not definition.manual_enabled and not l32_agent_draft:
        raise HTTPException(status_code=409, detail=f"{definition.name}尚未通过当前执行引擎验证")
    _validate_operation_geometry(job, definition, request.feature_ids)
    _validate_reference_profile(job, request.reference_profile_id)
    try:
        tool = get_tool(request.tool_id or definition.tool.default_tool_id)
        if tool.kind not in definition.tool.accepts:
            raise ValueError(f"刀具类型 {tool.kind} 不适用于 {definition.name}")
        existing = [operation for item in job.plan.setups for operation in item.operations] if job.plan else []
        next_sequence = max((operation.sequence for operation in existing), default=0) + 10
        insert_index = len(setup.operations)
        neighbour = setup.operations[-1] if setup.operations else None
        if request.insert_after_operation_id is not None:
            after_index = next((index for index,item in enumerate(setup.operations) if item.id == request.insert_after_operation_id),None)
            if after_index is None:
                raise ValueError("插入位置不属于当前装夹")
            insert_index = after_index+1
            neighbour = setup.operations[after_index]
        operation = create_operation_instance(
            id=f"OP{next_sequence}", sequence=next_sequence, type=definition.id,
            name=request.name or definition.name, feature_ids=request.feature_ids, tool=tool,
            parameters=request.parameters,
            rationale=request.rationale or [
                "Harness 基于当前几何、设备和已验证工序状态提出"
                if request.source == "recommendation"
                else "制造工程师从工序库手动创建"
            ],
            confidence=0.8 if request.source == "recommendation" else 1.0,
            status="proposed", source=request.source,
            channel_id=request.channel_id or (neighbour.channel_id if l32_agent_draft and neighbour else None),
            spindle_id=request.spindle_id or (neighbour.spindle_id if l32_agent_draft and neighbour else None),
            workpiece_side=request.workpiece_side or (neighbour.workpiece_side if l32_agent_draft and neighbour else None),
        )
        operation.reference_profile_id = request.reference_profile_id
        apply_cutting_parameters(operation, resolve_material(job.material), resolve_machine(job.machine))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    setup.operations.insert(insert_index,operation)
    for index,item in enumerate(setup.operations,1):
        item.sequence = index*10
    saved = _save_manual_plan_change(job_id, job)
    if request.source == "recommendation":
        publish_job_event(
            job_id, "harness_plan", f"Harness 已提出工序 {operation.id} · {operation.name}", 50,
            agent_node="harness_add_operation", agent_title="逐道建立工序",
            agent_kind="decision", agent_status="completed", operation_id=operation.id,
            evidence=[
                {"label": "工序类型", "value": operation.type},
                {"label": "刀具", "value": operation.tool.id},
                {"label": "几何引用", "value": operation.feature_ids},
            ],
            viewer={"kind": "operation", "operation_id": operation.id, "mode": "意图"},
        )
    return saved


@app.patch("/api/v1/jobs/{job_id}/setups/{setup_id}/operations/{operation_id}", response_model=JobResponse)
def update_manual_operation(job_id: str, setup_id: str, operation_id: str, request: OperationUpdateRequest) -> JobResponse:
    job = load_job(job_id)
    setup = _find_setup(job, setup_id)
    operation = next((item for item in setup.operations if item.id == operation_id), None)
    if operation is None:
        raise HTTPException(status_code=404, detail="工序不存在")
    if (
        operation.type in {"turn_threading", "turn_id_roughing", "turn_id_finishing", "axial_drilling", "turn_grooving"}
        and operation.source == "automatic"
        and (request.parameters is not None or request.tool_id is not None or request.enabled is not None)
    ):
        raise HTTPException(status_code=409, detail="自动规划的螺纹/内孔/外槽工序必须通过专用工程审核接口修改或启用")
    try:
        definition = get_operation_definition(operation.definition_id or operation.type)
        if request.feature_ids is not None:
            _validate_operation_geometry(job, definition, request.feature_ids)
            operation.feature_ids = request.feature_ids
        if request.reference_profile_id is not None:
            _validate_reference_profile(job, request.reference_profile_id)
            operation.reference_profile_id = request.reference_profile_id
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
    manifest_path = directory / "cam-manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("plan_sha256") != cam_plan_fingerprint(load_job(job_id)):
            raise HTTPException(status_code=409, detail="CAM artifacts belong to an older process plan")
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


def _run_operation_execution_agent(
    job_id: str,
    job: JobResponse,
    result: dict[str, object],
    verification: dict[str, object],
    simulation: dict[str, object],
    collision: dict[str, object],
    remediation: dict[str, object],
    *,
    cam_progress_callback: Callable[..., None] | None = None,
) -> dict[str, object] | None:
    from .agent.config import load_agent_settings
    from .agent.execution_graph import run_operation_execution_subgraph
    from .agent.operation_review import review_operation_evidence
    from .qwen import load_qwen_settings

    settings = load_agent_settings()
    if not settings.enabled or not job.plan:
        return None
    operations = [
        {
            **operation.model_dump(mode="json"),
            "setup_id": setup.id,
        }
        for setup in job.plan.setups
        for operation in setup.operations
        if operation.enabled
    ]
    ai_review_enabled = (
        settings.writes_production_results
        and isinstance(job.plan.ai_planning, dict)
        and job.plan.ai_planning.get("planner") == "langgraph_ai_primary"
    )
    qwen_settings = load_qwen_settings() if ai_review_enabled else None
    model_view = job_directory(job_id) / "agent-perception-contact-sheet.png"

    def review_record(record: dict[str, object]) -> dict[str, object]:
        return review_operation_evidence(
            str(record["operation_id"]),
            {
                "operation": record.get("operation"),
                "evidence": record.get("evidence"),
                "defects": record.get("defects"),
                "collisions": record.get("collisions"),
                "low_rapids": record.get("low_rapids"),
                "verification_status": verification.get("status"),
                "stock": job.plan.stock.model_dump(mode="json") if hasattr(job.plan.stock, "model_dump") else job.plan.stock,
                "geometry_measurements": job.analysis.measurements if job.analysis else None,
            },
            model_view=model_view if model_view.is_file() else None,
            settings=qwen_settings,
        )

    def report(stage: str, message: str, **details: object) -> None:
        operation_id = details.get("operation_id")
        feature_ids = details.get("feature_ids")
        terminal = stage in {"review_operation", "summarize_execution"}
        viewer: dict[str, object] | None = None
        if operation_id:
            viewer = {
                "kind": "operation", "operation_id": operation_id,
                "feature_ids": feature_ids if isinstance(feature_ids, list) else [],
                "mode": "仿真",
            }
        publish_job_event(
            job_id, "operation_execution", message, 100,
            agent_node=stage,
            agent_title=("逐工序执行汇总" if stage == "summarize_execution" else f"工序验证 · {operation_id or stage}"),
            agent_kind="validation" if terminal else "tool_result",
            agent_status="completed" if terminal else "running",
            viewer=viewer,
            evidence=[
                {"label": key, "value": value}
                for key, value in details.items()
                if key not in {"feature_ids"} and isinstance(value, (str, int, float, bool))
            ],
            **details,
        )
        if cam_progress_callback:
            cam_progress_callback(
                "agent_execution", message, 99,
                agent_node=stage, operation_id=operation_id,
                **{key: value for key, value in details.items() if key != "operation_id"},
            )

    trace = run_operation_execution_subgraph(
        job_id=job_id,
        operations=operations,
        cam_result=result,
        simulation=simulation,
        verification=verification,
        collision=collision,
        remediation=remediation,
        settings=settings,
        progress_callback=report,
        review_callback=review_record if ai_review_enabled else None,
    )
    payload = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "mode": settings.mode,
        "review_mode": "multimodal_ai" if ai_review_enabled and model_view.is_file() else "structured_ai" if ai_review_enabled else "deterministic_rules",
        "status": trace.get("status"),
        "summary": trace.get("summary", {}),
        "records": trace.get("records", []),
    }
    write_json(job_directory(job_id) / "agent-execution.json", payload)
    from .agent.world_model import (
        apply_execution_trace,
        create_manufacturing_world_model,
        load_world_model,
    )
    world_path = job_directory(job_id) / "agent-world-model.json"
    world = load_world_model(world_path)
    if world is None and job.analysis and job.plan:
        world = create_manufacturing_world_model(
            job_id=job_id,
            analysis=job.analysis,
            plan=job.plan,
            material=job.material,
            machine=job.machine,
            filename=job.filename,
            model_url=job.model_url,
        )
    if world is not None:
        world = apply_execution_trace(world, payload)
        write_json(world_path, world.model_dump(mode="json"))
        orchestrator_path = job_directory(job_id) / "agent-orchestrator.json"
        orchestrator_payload = (
            json.loads(orchestrator_path.read_text(encoding="utf-8"))
            if orchestrator_path.is_file() else {
                "schema_version": "1.0.0", "job_id": job_id, "trace": [],
            }
        )
        trace = list(orchestrator_payload.get("trace", []))
        trace.append({
            "sequence": len(trace) + 1,
            "at": world.updated_at,
            "action": "reconcile_execution",
            "status": "completed",
            "summary": "已将真实刀路、仿真与安全校验结果写回制造世界模型",
        })
        orchestrator_payload.update({
            "status": world.lifecycle,
            "next_action": world.next_action,
            "trace": trace,
        })
        write_json(orchestrator_path, orchestrator_payload)
    publish_job_event(
        job_id, "operation_execution", "逐工序执行记录已归档", 100,
        agent_node="archive_execution_trace", agent_title="逐工序执行记录",
        agent_kind="result", agent_status="completed",
        evidence=[
            {"label": "状态", "value": payload.get("status")},
            {"label": "工序数", "value": len(payload["records"])},
        ],
        artifacts=[{
            "id": "agent-execution", "label": "逐工序执行记录", "kind": "json",
            "url": f"/api/v1/jobs/{job_id}/files/agent-execution.json",
        }],
    )
    return payload


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
        agent_execution = _run_operation_execution_agent(
            job_id, job, result, verification, simulation, collision, remediation,
            cam_progress_callback=report,
        )
        save_job(directory, job)
        write_json(directory / "plan.json", job.plan.model_dump(mode="json"))
        archive_cam_plan_fingerprint(directory, job)
        report(
            "completed", "薄板成形工序与分阶段仿真已生成", 100,
            generated=len(operations), verification=verification["status"], collision=collision["status"],
        )
        response = cam_response(job_id, result, verification, simulation, collision, remediation)
        if agent_execution:
            response["agent_execution"] = agent_execution
        return response
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
    agent_execution = _run_operation_execution_agent(
        job_id, job, result, verification, simulation, collision, remediation,
        cam_progress_callback=report,
    )
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
    archive_cam_plan_fingerprint(directory, job)
    response = cam_response(
        job_id, result, verification, simulation, collision, remediation,
        stdout=completed.stdout[-1000:],
    )
    if agent_execution:
        response["agent_execution"] = agent_execution
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
    """Run the durable validation/remediation graph until it converges or blocks."""

    from .agent.config import load_agent_settings
    from .agent.remediation_graph import run_validation_remediation_subgraph

    def report(stage: str, message: str, percent: float, **details: object) -> None:
        if progress_callback:
            progress_callback({
                "stage": stage,
                "message": message,
                "percent": round(max(0.0, min(percent, 100.0)), 1),
                **details,
            })

    directory = job_directory(job_id)
    remediation_path = directory / "remediation.json"
    if not remediation_path.is_file():
        raise HTTPException(status_code=409, detail="请先生成刀路和仿真，再启动自动纠错")
    current_report = json.loads(remediation_path.read_text(encoding="utf-8"))
    if not current_report.get("can_auto_replan"):
        raise HTTPException(status_code=409, detail="当前缺陷不能进入自动纠错闭环")
    settings = load_agent_settings()

    def graph_report(stage: str, message: str, **details: object) -> None:
        iteration = int(details.get("iteration", 0) or 0)
        max_iterations = max(int(details.get("max_iterations", settings.max_local_retries) or settings.max_local_retries), 1)
        base_percent = {
            "attribute_defect": 2,
            "choose_repair_scope": 4,
            "replan_local": 7,
            "regenerate_and_validate": 94,
            "assess_revalidation": 96,
            "finalize_remediation": 98,
        }.get(stage, 1)
        if iteration:
            base_percent = min(((iteration - 1) / max_iterations) * 94 + base_percent / max_iterations, 98)
        terminal = stage == "finalize_remediation"
        operation_ids = details.get("operation_ids")
        operation_id = operation_ids[0] if isinstance(operation_ids, list) and len(operation_ids) == 1 else None
        event_details = {
            ("remediation_status" if key == "status" else key): value
            for key, value in details.items()
        }
        publish_job_event(
            job_id, "validation_remediation", message, base_percent,
            agent_node=stage,
            agent_title=("验证纠错汇总" if terminal else message),
            agent_kind="result" if terminal else "validation",
            agent_status="completed" if terminal else "running",
            viewer={"kind": "operation", "operation_id": operation_id, "mode": "仿真"} if operation_id else None,
            evidence=[
                {"label": key, "value": value}
                for key, value in details.items()
                if isinstance(value, (str, int, float, bool))
            ],
            **event_details,
        )
        stream_stage = (
            "remediation_applied" if stage == "replan_local"
            else "iteration_retry" if stage == "assess_revalidation" and details.get("decision") == "retry"
            else stage
        )
        report(stream_stage, message, base_percent, **details)

    def apply_safe_repairs(_: dict[str, object]) -> dict[str, object]:
        return _apply_cam_remediation(job_id, auto_approve=True)

    def regenerate_and_validate(iteration: int, max_iterations: int) -> dict[str, object]:
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

        return _create_cam_artifact(job_id, progress_callback=forward_cam_progress)

    trace = run_validation_remediation_subgraph(
        job_id=job_id,
        remediation=current_report,
        settings=settings,
        apply_remediation=apply_safe_repairs,
        regenerate_cam=regenerate_and_validate,
        progress_callback=graph_report,
    )
    latest_result = trace.get("latest_result")
    if not isinstance(latest_result, dict):
        raise HTTPException(status_code=500, detail="纠错子图未产生复验结果")
    summary = trace.get("summary", {})
    outcome = str(summary.get("outcome", "manual_review"))
    iteration = int(summary.get("iteration", 0) or 0)
    payload = {
        "schema_version": "1.0.0",
        "job_id": job_id,
        "mode": settings.mode,
        "status": trace.get("status"),
        "summary": summary,
        "cycles": trace.get("cycles", []),
        "remaining_report": trace.get("current_report", {}),
    }
    write_json(directory / "agent-remediation.json", payload)
    publish_job_event(
        job_id, "validation_remediation", "验证纠错记录已归档", 100,
        agent_node="archive_remediation_trace", agent_title="验证纠错记录",
        agent_kind="result", agent_status="completed",
        evidence=[
            {"label": "结果", "value": outcome},
            {"label": "迭代次数", "value": iteration},
        ],
        artifacts=[{
            "id": "agent-remediation", "label": "验证纠错记录", "kind": "json",
            "url": f"/api/v1/jobs/{job_id}/files/agent-remediation.json",
        }],
    )
    messages = {
        "passed": f"自动纠错在第 {iteration} 轮通过全部复验",
        "blocked": f"第 {iteration} 轮发现高风险问题，已停止自动纠错",
        "max_iterations": f"已完成 {iteration} 轮自动纠错，问题尚未收敛",
        "manual_review": f"第 {iteration} 轮仍有需要工程师处理的问题",
    }
    defects = trace.get("current_report", {}).get("defects", [])
    report(
        "completed", messages.get(outcome, messages["manual_review"]), 100,
        mode="remediation_graph", outcome=outcome,
        iteration=iteration, max_iterations=summary.get("max_iterations"),
        defect_count=len(defects) if isinstance(defects, list) else 0,
    )
    return {**latest_result, "remediation_loop": {"outcome": outcome, "iteration": iteration}}


def _create_cam_with_agent_loop(
    job_id: str,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    """Generate, observe and safely repair CAM without a second user action.

    The initial CAM completion event is held until the bounded remediation graph
    either converges or reaches a safety gate, so streaming clients see one
    coherent agent run instead of a misleading early success.
    """
    from .agent.config import load_agent_settings

    terminal_event: dict[str, object] | None = None

    def forward_initial(event: dict[str, object]) -> None:
        nonlocal terminal_event
        if event.get("stage") == "completed":
            terminal_event = event
            return
        if progress_callback:
            progress_callback(event)

    result = _create_cam_artifact(
        job_id,
        progress_callback=forward_initial if progress_callback else None,
    )
    remediation = result.get("remediation")
    settings = load_agent_settings()
    can_auto_replan = (
        settings.mode == "active"
        and isinstance(remediation, dict)
        and remediation.get("can_auto_replan") is True
    )
    if can_auto_replan:
        if progress_callback:
            progress_callback({
                "stage": "agent_remediation_start",
                "message": "检测到可安全修复的问题，智能体开始局部调整并重新仿真",
                "percent": 1,
                "defect_count": len(remediation.get("defects", [])),
                "max_iterations": remediation.get("max_iterations", settings.max_local_retries),
            })
        return _run_cam_remediation_loop(job_id, progress_callback=progress_callback)

    if progress_callback:
        execution_status = (result.get("agent_execution") or {}).get("status")
        remediation_status = remediation.get("status") if isinstance(remediation, dict) else None
        review_required = execution_status in {"blocked", "action_required"} or remediation_status in {"blocked", "action_required"}
        progress_callback({
            **(terminal_event or {"stage": "completed", "percent": 100}),
            "message": "逐工序审核未通过，需要复核" if review_required else "CAM 刀路、逐工序材料状态与安全验证已归档",
            "agent_outcome": (
                "manual_review" if execution_status == "blocked" or remediation_status == "blocked"
                else "engineering_review" if execution_status == "action_required" or remediation_status == "action_required" else "passed"
            ),
            "execution_status": execution_status,
        })
    return result


@app.post("/api/v1/jobs/{job_id}/cam")
def create_cam_artifact(job_id: str) -> dict[str, object]:
    return _create_cam_with_agent_loop(job_id)


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
            _create_cam_with_agent_loop(job_id, progress_callback=events.put)
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
