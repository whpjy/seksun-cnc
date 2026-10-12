"""Reidentify into a new task without overwriting historical machining evidence."""
import shutil
import uuid
from pathlib import Path
from fastapi import APIRouter,HTTPException

router=APIRouter()


@router.post("/api/v1/jobs/{job_id}/geometry-revision")
def create_geometry_revision(job_id: str):
    from .main import (load_job,job_directory,save_job,utc_now,run_geometry_analyzer,
        persist_rotational_analysis,initialize_harness_process_draft,write_json)
    original=load_job(job_id)
    source_dir=job_directory(job_id)
    if original.device_id!="citizen-cincom-l32" or original.status!="completed" or original.analysis is None:
        raise HTTPException(409,"需要已完成分析的 L32 任务")
    if Path(original.filename).name!=original.filename or not (source_dir/original.filename).is_file():
        raise HTTPException(409,"原始 STEP 不可用")
    id=uuid.uuid4().hex
    directory=job_directory(id)
    directory.mkdir()
    revised=original.model_copy(deep=True,update={"id":id,"status":"processing","created_at":utc_now(),
        "analysis":None,"plan":None,"error":None,"model_url":None})
    shutil.copyfile(source_dir/original.filename,directory/original.filename)
    for name in ("drawing.pdf","drawing-analysis.json","machine-configuration.json"):
        if (source_dir/name).is_file():shutil.copyfile(source_dir/name,directory/name)
    save_job(directory,revised)
    try:
        analysis=run_geometry_analyzer(directory/revised.filename,directory/"analysis.json",directory/"model.stl",
            int(original.analysis.topology.get("selected_solid_index",0)) or None)
        revised.analysis=analysis
        revised.status="completed"
        revised.model_url=f"/api/v1/jobs/{id}/files/model.stl"
        persist_rotational_analysis(directory,revised,analysis)
        save_job(directory,revised)
        revised=initialize_harness_process_draft(id)
        revised.plan.title=f"修复重识别 · {revised.filename}（工序待重算）"
        revised.plan.ai_planning.update(geometry_revision_source_job_id=job_id,
            geometry_profile_algorithm="section-curve-intersections-v2")
        save_job(directory,revised)
        write_json(directory/"plan.json",revised.plan.model_dump(mode="json"))
        return {"job_id":id,"source_job_id":job_id,"filename":revised.filename,
            "status":"geometry_reidentified","operation_count":0,"release_status":"DRAFT",
            "production_ready":False,"next_action":"open_job_context_then_plan_operations",
            "task_url":f"/?task={id}"}
    except Exception:
        revised.status="failed"
        revised.error="新版本几何分析未完成，原任务未修改。"
        save_job(directory,revised)
        raise
