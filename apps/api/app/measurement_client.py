from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


class MeasurementServiceError(RuntimeError):
    pass


def analyze_pdf_step(
    base_url: str, pdf_path: Path, step_path: Path, *, timeout_seconds: float = 600,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not base_url.strip():
        raise MeasurementServiceError("CNC_MEAS_API_BASE_URL is not configured")
    try:
        with pdf_path.open("rb") as pdf_stream, step_path.open("rb") as step_stream:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{base_url.rstrip('/')}/api/v1/comparisons",
                    files={
                        "pdf": (pdf_path.name, pdf_stream, "application/pdf"),
                        "step": (step_path.name, step_stream, "application/step"),
                    },
                )
    except (OSError, httpx.RequestError) as error:
        raise MeasurementServiceError(f"Measurement service connection failed: {type(error).__name__}") from error
    if not response.is_success:
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = response.text
        raise MeasurementServiceError(f"Measurement service rejected drawing: {str(detail)[:500]}")
    try:
        payload = response.json()
        specification = payload["comparison"]["manufacturing_specification"]
    except (ValueError, KeyError, TypeError) as error:
        raise MeasurementServiceError("Measurement service returned no manufacturing specification") from error
    if not isinstance(specification, dict):
        raise MeasurementServiceError("Measurement specification has an invalid format")
    metadata = {
        "measurement_job_id": payload.get("id"),
        "status": payload.get("status"),
        "elapsed_seconds": payload.get("elapsed_seconds"),
        "results": payload.get("results"),
    }
    return specification, metadata
