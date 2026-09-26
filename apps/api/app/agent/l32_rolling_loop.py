"""Persisted, evidence-gated rolling window for L32 operation validation.

The expensive whole-program compile and continuous-stock simulation remain the
source of truth.  This module exposes that evidence one operation at a time so
an agent must review and accept the current operation before moving forward.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def plan_signature(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def operation_sequence(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "operation_id": str(operation.get("id", "")),
            "operation_name": operation.get("name"),
            "operation_type": operation.get("type"),
            "setup_id": setup.get("id"),
        }
        for setup in plan.get("setups", [])
        for operation in setup.get("operations", [])
        if operation.get("enabled", True) and operation.get("id")
    ]


def advance_l32_rolling_loop(
    *,
    job_id: str,
    plan: dict[str, Any],
    validation: dict[str, Any],
    previous: dict[str, Any] | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Advance exactly one operation using a previously generated evidence set."""
    signature = plan_signature(plan)
    sequence = operation_sequence(plan)
    operation_ids = [item["operation_id"] for item in sequence]
    same_plan = bool(previous and previous.get("plan_signature") == signature and not reset)
    accepted = [
        str(item) for item in (previous or {}).get("accepted_operation_ids", [])
        if str(item) in operation_ids
    ] if same_plan else []
    history = list((previous or {}).get("history", [])) if same_plan else []
    next_operation = next((item for item in sequence if item["operation_id"] not in accepted), None)

    if next_operation is None:
        return {
            "schema_version": "1.0.0", "job_id": job_id, "plan_signature": signature,
            "status": "completed", "release_status": "DRAFT", "production_ready": False,
            "accepted_operation_ids": accepted, "accepted_count": len(accepted),
            "expected_operation_count": len(sequence), "current_operation": None,
            "next_operation_id": None, "history": history,
            "next_action": "machine_level_validation",
            "message": "所有具有仿真证据的计划工序均已逐项接受；仍不代表生产放行。",
        }

    operation_id = next_operation["operation_id"]
    record = next((
        item for item in validation.get("operations", [])
        if isinstance(item, dict) and str(item.get("operation_id")) == operation_id
    ), None)
    now = utc_now()
    if record is None:
        blocker = str(validation.get("error") or "当前工序没有可审计的连续材料仿真证据")
        decision = {
            "at": now, "operation_id": operation_id, "decision": "waiting_evidence",
            "reason": "missing_operation_evidence", "detail": blocker[:1000],
            "blocked_scope": "plan",
        }
        history.append(decision)
        status = "plan_blocked"
        next_action = str(validation.get("next_action") or "human_review")
    else:
        record_status = str(record.get("status") or "blocked")
        if record_status == "passed":
            accepted.append(operation_id)
            decision = {
                "at": now, "operation_id": operation_id, "decision": "accepted",
                "reason": "deterministic_and_agent_evidence_passed",
            }
            history.append(decision)
            status = "completed" if len(accepted) == len(sequence) else "ready"
            next_action = "complete" if status == "completed" else "advance_next_operation"
        else:
            decision = {
                "at": now, "operation_id": operation_id, "decision": "blocked",
                "reason": record_status,
                "detail": "; ".join(str(item) for item in record.get("blocking_reasons", []))[:1000],
            }
            history.append(decision)
            status = "blocked"
            next_action = str(validation.get("next_action") or "revise_current_operation")

    following = next((item for item in sequence if item["operation_id"] not in accepted), None)
    return {
        "schema_version": "1.0.0", "job_id": job_id, "plan_signature": signature,
        "status": status, "release_status": "DRAFT", "production_ready": False,
        "accepted_operation_ids": accepted, "accepted_count": len(accepted),
        "expected_operation_count": len(sequence), "current_operation": next_operation,
        "current_evidence": record, "decision": decision,
        "next_operation_id": following["operation_id"] if following else None,
        "history": history, "next_action": next_action,
        "validation_status": validation.get("status"),
        "validation_artifacts": validation.get("artifacts") or [],
        "message": (
            f"{operation_id} 已接受，可推进下一道工序。"
            if decision["decision"] == "accepted"
            else (
                f"{operation_id} 正在等待上游工艺方案补全；当前工序本身尚未判定失败。"
                if decision["decision"] == "waiting_evidence"
                else f"{operation_id} 未通过证据门禁，滚动规划已停止。"
            )
        ),
    }
