from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.agent.operation_review import review_operation_evidence
from app.qwen import QwenPlanningError, QwenSettings


def test_operation_review_sends_real_model_view_and_checks_operation_id(tmp_path: Path) -> None:
    view = tmp_path / "model.png"
    Image.new("RGB", (2, 2), "white").save(view)
    settings = QwenSettings(
        api_key="test-key", base_url="https://example.test/v1",
        model="test-model", timeout_seconds=3,
    )
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "operation_id": "OP10", "verdict": "passed", "confidence": 0.84,
            "reasoning": "The reported cut and stock metrics are consistent.",
            "observed_evidence": ["2 cut segments"], "risks": [],
            "missing_evidence": [], "suggested_changes": [],
        })}}]})

    result = review_operation_evidence(
        "OP10", {"evidence": {"cut_segment_count": 2}},
        model_view=view, settings=settings, transport=httpx.MockTransport(handler),
    )

    assert result["review"]["verdict"] == "passed"
    assert result["visual_scope"] == "source_model_only"
    assert requests[0]["messages"][1]["content"][1]["type"] == "image_url"

    with pytest.raises(QwenPlanningError, match="编号"):
        review_operation_evidence(
            "OP20", {}, model_view=view, settings=settings,
            transport=httpx.MockTransport(handler),
        )
