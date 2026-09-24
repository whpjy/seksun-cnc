from __future__ import annotations

import json
import struct

import httpx

from app.agent.config import load_agent_settings
from app.agent.orchestrator import OrchestratorTools, run_manufacturing_orchestrator
from app.agent.perception import build_perception_tool, render_model_evidence
from app.agent.world_model import (
    ManufacturingWorldModel, OpenQuestion, WorldOperation,
)
from app.qwen import QwenSettings


def _model(path) -> None:
    vertices = (
        ((0, 0, 0), (12, 0, 0), (0, 8, 0)),
        ((0, 0, 0), (0, 8, 0), (0, 0, 6)),
        ((0, 0, 0), (0, 0, 6), (12, 0, 0)),
        ((12, 0, 0), (0, 0, 6), (0, 8, 0)),
    )
    data = bytearray(80) + struct.pack("<I", len(vertices))
    for triangle in vertices:
        data.extend(struct.pack("<12fH", 0, 0, 1, *(value for vertex in triangle for value in vertex), 0))
    path.write_bytes(data)


def _world() -> ManufacturingWorldModel:
    return ManufacturingWorldModel(
        job_id="vision-test",
        current_objective="核对首道工序",
        current_operation_id="OP10",
        next_action="perceive",
        operations=[WorldOperation(id="OP10", setup_id="S1", name="首道加工", sequence=1)],
        open_questions=[OpenQuestion(
            id="initial-model-understanding", question="是否可达？",
            reason="加工前核对", blocking=True,
        )],
    )


def _settings(tmp_path):
    return load_agent_settings({
        "CNC_AGENT_MODE": "shadow",
        "CNC_AGENT_CHECKPOINT_PATH": str(tmp_path / "perception.sqlite"),
    })


def test_qwen_perception_uses_real_image_and_updates_world(tmp_path) -> None:
    _model(tmp_path / "model.stl")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert "是否可达" in payload["messages"][1]["content"][0]["text"]
        review = {
            "schema_version": "1.0.0",
            "question_id": "initial-model-understanding",
            "answer": "结构化几何和四视图支持当前装夹方向",
            "resolved": True,
            "confidence": 0.83,
            "findings": [{
                "category": "accessibility", "statement": "当前方向可达",
                "confidence": 0.83, "evidence_view_ids": ["front", "isometric"],
                "feature_ids": [], "planning_impact": "继续编译 OP10",
            }],
            "risks": [], "missing_evidence": [],
        }
        return httpx.Response(200, json={
            "id": "vision-1", "model": "qwen3.8-max-0902",
            "choices": [{"message": {"content": json.dumps(review)}}],
        })

    tool = build_perception_tool(
        tmp_path,
        settings=QwenSettings("test-key", "https://example.invalid/v1", "qwen3.8-max-0902", 15),
        transport=httpx.MockTransport(handler),
    )
    result = run_manufacturing_orchestrator(
        world=_world(), settings=_settings(tmp_path),
        tools=OrchestratorTools(perceive=tool), max_steps=8,
    )
    world = ManufacturingWorldModel.model_validate(result["world"])
    assert len(requests) == 1
    assert result["status"] == "waiting"
    assert world.open_questions[0].status == "resolved"
    assert world.next_action == "compile"
    assert any(item.kind == "ai_review" for item in world.evidence)
    assert (tmp_path / "agent-perception.json").is_file()
    assert (tmp_path / "agent-perception-contact-sheet.png").is_file()


def test_low_confidence_perception_keeps_question_open(tmp_path) -> None:
    _model(tmp_path / "model.stl")

    def handler(_request: httpx.Request) -> httpx.Response:
        review = {
            "schema_version": "1.0.0", "question_id": "initial-model-understanding",
            "answer": "视图无法确认刀具与夹具间隙", "resolved": True,
            "confidence": 0.34, "findings": [{
                "category": "uncertainty", "statement": "夹具未出现在模型中",
                "confidence": 0.34, "evidence_view_ids": [],
                "feature_ids": [], "planning_impact": "暂停执行",
            }], "risks": ["缺少夹具模型"],
            "missing_evidence": ["夹具模型"],
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(review)}}]})

    tool = build_perception_tool(
        tmp_path,
        settings=QwenSettings("test-key", "https://example.invalid/v1", "qwen3.8-max-0902", 15),
        transport=httpx.MockTransport(handler),
    )
    result = run_manufacturing_orchestrator(
        world=_world(), settings=_settings(tmp_path),
        tools=OrchestratorTools(perceive=tool), max_steps=8,
    )
    world = ManufacturingWorldModel.model_validate(result["world"])
    assert result["status"] == "waiting"
    assert world.open_questions[0].status == "open"
    assert world.next_action == "perceive"
    assert world.risks[0]["description"] == "缺少夹具模型"


def test_renderer_rejects_empty_stl(tmp_path) -> None:
    (tmp_path / "model.stl").write_bytes(b"solid empty\nendsolid empty\n")
    try:
        render_model_evidence(tmp_path / "model.stl", tmp_path)
    except ValueError as error:
        assert "no valid renderable triangles" in str(error)
    else:
        raise AssertionError("empty geometry was rendered")
