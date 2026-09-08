from pathlib import Path
import subprocess

from app import engines


def test_resolve_executable_returns_none_for_missing_command(monkeypatch):
    monkeypatch.setattr(engines.shutil, "which", lambda _: None)
    assert engines.resolve_executable("definitely-missing-cam-engine") is None


def test_run_camotics_uses_binary_reduced_setup_surface(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(engines, "resolve_executable", lambda _: "/usr/local/bin/camsim")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(engines.subprocess, "run", fake_run)
    gcode = tmp_path / "SETUP1.camotics"
    surface = tmp_path / "camotics-SETUP1.stl"
    engines.run_camotics("camsim", gcode, surface, resolution_mm=0.4)

    assert captured["command"] == [
        "/usr/local/bin/camsim", "--binary", "--reduce", "--resolution", "0.4",
        str(gcode), str(surface),
    ]
    assert captured["kwargs"]["check"] is True
