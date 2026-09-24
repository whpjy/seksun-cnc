from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class EngineProbe:
    id: str
    role: str
    command: str
    available: bool
    executable: str | None
    version: str | None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "command": self.command,
            "available": self.available,
            "executable": self.executable,
            "version": self.version,
            "detail": self.detail,
        }


def resolve_executable(command: str) -> str | None:
    resolved = shutil.which(command)
    if resolved:
        return resolved
    candidate = Path(command)
    return str(candidate) if candidate.is_file() else None


def probe_engine(engine_id: str, role: str, command: str, version_args: Sequence[str]) -> EngineProbe:
    executable = resolve_executable(command)
    if not executable:
        return EngineProbe(engine_id, role, command, False, None, None, "executable_not_found")
    try:
        completed = subprocess.run(
            [executable, *version_args],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        )
        output = (completed.stdout or completed.stderr).strip()
        first_line = next((line.strip() for line in output.splitlines() if line.strip()), None)
        return EngineProbe(
            engine_id,
            role,
            command,
            completed.returncode == 0,
            executable,
            first_line,
            None if completed.returncode == 0 else f"version_probe_exit_{completed.returncode}",
        )
    except (OSError, subprocess.SubprocessError) as error:
        return EngineProbe(engine_id, role, command, False, executable, None, str(error))


def run_freecad_adapter(
    command: str,
    adapter_script: Path,
    arguments: Sequence[Path],
    timeout_seconds: int = 600,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    trial_mode: bool = False,
) -> subprocess.CompletedProcess[str]:
    executable = resolve_executable(command)
    if not executable:
        raise FileNotFoundError(f"FreeCAD executable not found: {command}")
    environment = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "CNC_CAM_ARGUMENTS_JSON": json.dumps([str(argument) for argument in arguments]),
        "CNC_CAM_TRIAL_MODE": "1" if trial_mode else "0",
    }
    script = str(adapter_script)
    python_command = f"exec(compile(open({script!r}, encoding='utf-8').read(), {script!r}, 'exec'))"
    invocation = [executable, "-c", python_command]
    if progress_callback is None:
        return subprocess.run(
            invocation,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )

    process = subprocess.Popen(
        invocation,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=environment,
    )
    output_lines: list[str] = []
    assert process.stdout is not None
    try:
        for line in process.stdout:
            output_lines.append(line)
            marker = "CNC_PROGRESS "
            if marker not in line:
                continue
            try:
                # FreeCAD occasionally writes diagnostics without a trailing
                # newline, so its text can be attached ahead of our marker.
                payload, _ = json.JSONDecoder().raw_decode(
                    line.split(marker, 1)[1].lstrip(),
                )
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                progress_callback(payload)
        return_code = process.wait(timeout=timeout_seconds)
    except BaseException:
        process.kill()
        process.wait()
        raise
    output = "".join(output_lines)
    if return_code:
        raise subprocess.CalledProcessError(return_code, invocation, output=output, stderr=output)
    return subprocess.CompletedProcess(invocation, return_code, stdout=output, stderr="")


def run_camotics(
    command: str,
    project_path: Path,
    output_path: Path,
    resolution_mm: float = 0.5,
    timeout_seconds: int = 600,
) -> subprocess.CompletedProcess[str]:
    """Run CAMotics as an isolated stock-removal engine for one setup."""
    executable = resolve_executable(command)
    if not executable:
        raise FileNotFoundError(f"CAMotics executable not found: {command}")
    return subprocess.run(
        [
            executable,
            "--binary",
            "--reduce",
            "--resolution",
            str(resolution_mm),
            str(project_path),
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
