import json

import httpx
import pytest

from app.models import GeometryAnalysis
from app.planner import build_process_plan
from app.qwen import (
    QwenPlanningError, QwenSettings, build_manufacturing_context, probe_qwen,
    qwen_config_payload, review_process_plan,
)


def settings(api_key: str = "test-key") -> QwenSettings:
    return QwenSettings(
        api_key=api_key,
        base_url="https://example.invalid/compatible-mode/v1",
        model="qwen3.8-max-0902",
        timeout_seconds=5,
        planning_thinking=True,
    )


def test_config_never_exposes_api_key() -> None:
    payload = qwen_config_payload(settings("super-secret"))
    assert payload["configured"] is True
    assert "super-secret" not in str(payload)


def test_probe_reports_missing_key_without_network_request() -> None:
    result = probe_qwen(settings(""))
    assert result["available"] is False
    assert result["configured"] is False


def test_probe_sends_minimal_non_thinking_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "qwen3.8-max-0902"
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 16
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "model": "qwen3.8-max-0902",
            "choices": [{"message": {"role": "assistant", "content": "QWEN_OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    result = probe_qwen(settings(), transport=httpx.MockTransport(handler))
    assert result["available"] is True
    assert result["reply"] == "QWEN_OK"
    assert result["request_id"] == "chatcmpl-test"


def test_probe_returns_sanitized_api_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(
        401, json={"error": {"code": "invalid_api_key", "message": "Invalid key"}},
    ))
    result = probe_qwen(settings(), transport=transport)
    assert result["available"] is False
    assert result["status_code"] == 401
    assert result["error"] == "invalid_api_key: Invalid key"
    assert "test-key" not in str(result)


def planning_input():
    analysis = GeometryAnalysis.model_validate({
        "schema_version": "1.0",
        "source_file": "fixture.step",
        "topology": {"solids": 1, "faces": 6},
        "measurements": {
            "volume": 1000,
            "surface_area": 600,
            "bounding_box": {
                "minimum": {"x": 0, "y": 0, "z": 0},
                "maximum": {"x": 10, "y": 10, "z": 10},
                "size": {"x": 10, "y": 10, "z": 10},
            },
        },
        "planar_features": [{
            "id": "PF-1", "area": 100, "center": {"x": 5, "y": 5, "z": 10},
            "normal": {"x": 0, "y": 0, "z": 1},
        }],
        "cylindrical_features": [],
        "visual_edges": [[{"x": 0, "y": 0, "z": 0}, {"x": 1, "y": 1, "z": 1}]],
    })
    return analysis, build_process_plan(analysis, "6061-T6", "VMC850")


def test_manufacturing_context_omits_large_visual_edge_payload() -> None:
    analysis, plan = planning_input()
    context = build_manufacturing_context(analysis, plan)
    assert "visual_edges" not in context["geometry"]
    assert context["geometry"]["omissions"]["visual_edges"] == "omitted_from_language_context"
    assert context["safety_policy"]["ai_must_not_generate_gcode"] is True
    assert 5 < len(context["manufacturing_process_knowledge"]["processes"]) < 98
    assert context["manufacturing_process_knowledge"]["retrieval"]["mode"] == "route_and_family_relevant"
    assert len(context["manufacturing_process_knowledge"]["typical_routes"]) == 8


def test_process_review_uses_strict_schema_and_validates_response() -> None:
    analysis, plan = planning_input()
    progress_events: list[tuple[str, str, dict[str, object]]] = []
    review = {
        "schema_version": "1.0.0",
        "manufacturing_intent": "fixture tooling",
        "part_family": "prismatic fixture",
        "recommended_process_kind": "subtractive",
        "deterministic_plan_assessment": "acceptable",
        "confidence": 0.91,
        "summary": "三轴铣削方案可继续复核。",
        "setup_strategy": ["以最大平面作为首装基准"],
        "route_recommendations": [{
            "process_code": "GX-C-07", "action": "keep", "stage": "roughing",
            "sequence": 20, "reason": "匹配当前铣削粗加工",
            "confidence": 0.8, "prerequisite_codes": [],
            "blocking_missing_information": ["尺寸公差"],
        }],
        "operation_recommendations": [],
        "risks": [{
            "severity": "medium", "code": "FIXTURE_REVIEW",
            "description": "夹紧区域尚未实测", "evidence": ["仅有 STEP 几何"],
            "recommended_action": "由工程师确认夹紧位置",
        }],
        "missing_information": ["尺寸公差"],
        "requires_engineer_review": True,
        "approval_blocked": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["enable_thinking"] is True
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "route_recommendations" in body["response_format"]["json_schema"]["schema"]["required"]
        assert "max_tokens" not in body
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        content = json.dumps(review, ensure_ascii=False)
        fragments = [content[index:index + 80] for index in range(0, len(content), 80)]
        stream = "".join(
            "data: " + json.dumps({
                "id": "review-1", "model": "qwen3.8-max-0902",
                "choices": [{"delta": {"content": fragment}}],
            }, ensure_ascii=False) + "\n\n"
            for fragment in fragments
        )
        stream += "data: " + json.dumps({
            "id": "review-1", "model": "qwen3.8-max-0902",
            "choices": [], "usage": {"total_tokens": 100},
        }) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    result = review_process_plan(
        analysis, plan, settings=settings(), transport=httpx.MockTransport(handler),
        progress_callback=lambda stage, message, **details: progress_events.append(
            (stage, message, details)
        ),
    )
    assert result["review"]["recommended_process_kind"] == "subtractive"
    assert result["review"]["route_recommendations"][0]["process_code"] == "GX-C-07"
    assert result["review"]["requires_engineer_review"] is True
    assert result["input_summary"]["operation_count"] == 1
    stages = [stage for stage, _message, _details in progress_events]
    assert stages[:3] == ["ai_context", "ai_request", "ai_waiting"]
    assert stages[-4:] == [
        "ai_response", "ai_schema_validation", "ai_capability_validation", "ai_review_completed",
    ]
    assert "ai_stream_manufacturing_intent" in stages
    assert "ai_stream_setup_strategy" in stages
    assert "ai_stream_operation_recommendations" in stages
    assert "ai_stream_risks" in stages
    assert progress_events[1][2]["operation_count"] == 1
    assert progress_events[1][2]["feature_count"] > 0
    assert "首个结构化审查字段" in progress_events[2][1]
    assert result["usage"]["total_tokens"] == 100


def test_process_review_rejects_unknown_operation_type() -> None:
    analysis, plan = planning_input()
    review = {
        "schema_version": "1.0.0",
        "manufacturing_intent": "fixture tooling",
        "part_family": "prismatic fixture",
        "recommended_process_kind": "subtractive",
        "deterministic_plan_assessment": "revise",
        "confidence": 0.8,
        "summary": "需要增加未受支持的工序。",
        "setup_strategy": ["三轴装夹"],
        "route_recommendations": [],
        "operation_recommendations": [{
            "action": "add",
            "operation_type": "imaginary_laser_polishing",
            "setup_id": "",
            "operation_id": "",
            "feature_ids": [],
            "reason": "模型幻觉示例",
            "priority": 1,
        }],
        "risks": [],
        "missing_information": [],
        "requires_engineer_review": True,
        "approval_blocked": True,
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={
        "id": "review-invalid-operation",
        "model": "qwen3.8-max-0902",
        "choices": [{"message": {"content": json.dumps(review, ensure_ascii=False)}}],
    }))

    with pytest.raises(QwenPlanningError, match="unsupported operation types"):
        review_process_plan(analysis, plan, settings=settings(), transport=transport)
