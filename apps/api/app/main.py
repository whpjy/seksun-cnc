from __future__ import annotations

import json
import hashlib
from math import isfinite, radians, tan
import os
import shutil
import subprocess
import tempfile
import threading
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
from .l32_configuration import l32_definition_payload, l32_viii_live_tool_catalog_reference, snapshot_l32_instance
from .l32_indexed_pocket import IndexedPocketDraft, IndexedPocketSweepCheck, build_indexed_back_pocket_draft
from .l32_side_milling import build_l32_exterior_clear_draft, build_l32_side_mill_draft
from .l32_front_groove import build_front_groove_geometry_draft
from .tool_inventory import ToolInventoryInput, ToolInventoryRecord, physical_tool_fit_for_groove, record_physical_tool
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
from .benchmarks import example_catalog_payload
from .coverage import evaluate_plan_coverage
from .remediation import apply_automatic_remediation, build_remediation_report
from .rotational_features import (
    RotationalFeatureAnalysis, bind_thread_requirements, infer_rotational_features,
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
    accepted_axis_ids = {
        profile.axis_id for profile in result.profiles
        if profile.review_state == "accepted"
    }
    for axis in result.axes:
        if axis.id in accepted_axis_ids:
            axis.review_state = "accepted"
            axis.review_reasons = []
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
        feature_id=feature_id,
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
        "l32-reference-sweep-v1", source,
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
    regional_backside = any(
        item.id == "OP58-BACK"
        for setup in job.plan.setups for item in setup.operations
    )
    for setup in job.plan.setups:
        for operation in setup.operations:
            if operation.workpiece_side != "back":
                continue
            if operation.type in {"pocket_roughing", "pocket_finishing"}:
                operation.enabled = back_live_tool_available
            else:
                operation.enabled = back_turning_enabled and not (
                    regional_backside and operation.id == "OP60"
                )
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
    redundant_cleanup_warning = "背面区域精车 OP58-BACK 已覆盖切断邻域；旧版 OP60 清根工序保持禁用以防重复过切。"
    if regional_backside and any(
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
                if selected_as_primary or promoted:
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
            if plan.process_kind == "subtractive" and job.device_id != "citizen-cincom-l32" and plan.automation_status != "unsupported" and resolve_executable(FREECAD_CMD):
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
            agent_kind="result", agent_status="completed",
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
    l32_turning_draft = (
        job.device_id == "citizen-cincom-l32"
        and definition.engine.provider == "turning"
    )
    if not definition.manual_enabled and not l32_turning_draft:
        raise HTTPException(status_code=409, detail=f"{definition.name}尚未通过当前执行引擎验证")
    _validate_operation_geometry(job, definition, request.feature_ids)
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
            parameters=request.parameters, rationale=["制造工程师从工序库手动创建"],
            confidence=1.0, status="proposed", source="manual",
            channel_id=neighbour.channel_id if l32_turning_draft and neighbour else None,
            spindle_id=neighbour.spindle_id if l32_turning_draft and neighbour else None,
            workpiece_side=neighbour.workpiece_side if l32_turning_draft and neighbour else None,
        )
        apply_cutting_parameters(operation, resolve_material(job.material), resolve_machine(job.machine))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    setup.operations.insert(insert_index,operation)
    for index,item in enumerate(setup.operations,1):
        item.sequence = index*10
    return _save_manual_plan_change(job_id, job)


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
