from app import main
from app.geometry_revision import create_geometry_revision
from test_l32_operation_trial import job,operation,rotational


def test_geometry_revision_preserves_original_and_starts_empty(monkeypatch,tmp_path):
    original=job(operation("OP10",10,"turn_facing"))
    original.filename="part.step"
    root=tmp_path/original.id
    root.mkdir()
    (root/original.filename).write_text("source STEP")
    (root/"drawing.pdf").write_bytes(b"original drawing")
    jobs={original.id:original}
    monkeypatch.setattr(main,"job_directory",lambda id:tmp_path/id)
    monkeypatch.setattr(main,"load_job",lambda id:jobs[id])
    monkeypatch.setattr(main,"save_job",lambda directory,value:jobs.__setitem__(value.id,value))
    monkeypatch.setattr(main,"run_geometry_analyzer",lambda *_:original.analysis.model_copy(deep=True))
    monkeypatch.setattr(main,"persist_rotational_analysis",lambda *_:rotational())
    def initialize(id):
        revised=jobs[id]
        revised.plan=original.plan.model_copy(deep=True)
        for setup in revised.plan.setups:setup.operations=[]
        return revised
    monkeypatch.setattr(main,"initialize_harness_process_draft",initialize)
    result=create_geometry_revision(original.id)
    assert result["job_id"]!=original.id
    assert result["operation_count"]==0 and result["production_ready"] is False
    assert len(original.plan.setups[0].operations)==1
    assert (tmp_path/result["job_id"]/"drawing.pdf").read_bytes()==b"original drawing"
    assert jobs[result["job_id"]].plan.ai_planning["geometry_revision_source_job_id"]==original.id
