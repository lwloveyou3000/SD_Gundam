"""Exercise Windows batch entry points through the real CMD parser."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
START = "启动挂机.cmd"
INSTALL = "安装依赖.cmd"
ANACONDA = Path(r"F:\ProgramData\anaconda3\python.exe")


def run_cmd(script, *arguments, timeout=120):
    return subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "call", str(script), *arguments],
        cwd=script.parent, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def copy_project_entries(destination):
    destination.mkdir(parents=True)
    for name in (START, INSTALL, "requirements.txt"):
        shutil.copyfile(ROOT / name, destination / name)
    # A harmless stand-in verifies the launched interpreter and path, without
    # starting another GUI or interacting with the user's running game.
    (destination / "app.py").write_text(
        "from pathlib import Path\nimport sys\n"
        "Path(__file__).with_name('launch-completed.txt').write_text(sys.executable, encoding='utf-8')\n",
        encoding="utf-8",
    )
    return destination


def test_batch_files_are_ascii_without_bom_and_use_crlf():
    for name in (START, INSTALL):
        data = (ROOT / name).read_bytes()
        assert data.decode("ascii").startswith("@echo off\r\n")
        assert b"\n" not in data.replace(b"\r\n", b"")
        assert not data.startswith(b"\xef\xbb\xbf")
    assert "*.cmd text eol=crlf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


windows_runtime = pytest.mark.skipif(
    os.name != "nt" or not ANACONDA.exists(),
    reason="Needs the configured Windows Python runtime for real launcher checks",
)


@windows_runtime
def test_incomplete_environment_falls_back_without_launching(tmp_path):
    project = copy_project_entries(tmp_path / "测试 空格 fallback")
    subprocess.run([str(ANACONDA), "-m", "venv", "--without-pip", str(project / ".venv")],
                   check=True, capture_output=True, timeout=60)
    # Keep the real CMD control flow, but provide the tested complete runtime
    # as the fallback. The unmodified base Anaconda may lack the OCR dependency.
    launcher = project / START
    text = launcher.read_text(encoding="ascii")
    text = text.replace(str(ANACONDA), sys.executable)
    text = text.replace(str(ANACONDA.with_name("pythonw.exe")),
                        str(Path(sys.executable).with_name("pythonw.exe")))
    launcher.write_bytes(text.replace("\n", "\r\n").encode("ascii"))
    result = run_cmd(project / START, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Launcher check passed: Anaconda" in result.stdout
    assert not (project / "launch-completed.txt").exists()


@windows_runtime
def test_missing_ocr_is_rejected_before_launching(tmp_path):
    project = copy_project_entries(tmp_path / "测试 缺少识别依赖")
    subprocess.run([str(ANACONDA), "-m", "venv", "--system-site-packages", "--without-pip",
                    str(project / ".venv")], check=True, capture_output=True, timeout=60)
    # An import guard forces only the new dependency to fail. Other GUI/image
    # dependencies remain usable; a successful launch here would be misleading.
    (project / "rapidocr_onnxruntime.py").write_text(
        "raise ImportError('OCR not installed')\n", encoding="ascii")
    result = run_cmd(project / START, "--check")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "No usable Python environment" in result.stdout
    assert not (project / "launch-completed.txt").exists()


@pytest.fixture
def installed_project(tmp_path):
    project = copy_project_entries(tmp_path / "测试 空格 installed")
    result = run_cmd(project / INSTALL, "--no-pause")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installation completed" in result.stdout
    return project


@windows_runtime
def test_installer_finishes_and_environment_is_selected(installed_project):
    result = run_cmd(installed_project / START, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Launcher check passed: project .venv" in result.stdout


@windows_runtime
def test_installer_reuses_environment_while_its_pythonw_is_open(installed_project):
    process = subprocess.Popen(
        [str(installed_project / ".venv" / "Scripts" / "pythonw.exe"),
         "-c", "import time; time.sleep(180)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = run_cmd(installed_project / INSTALL, "--no-pause")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Installation completed" in result.stdout
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


@windows_runtime
def test_normal_launcher_starts_app_from_unicode_space_path(installed_project):
    result = run_cmd(installed_project / START)
    assert result.returncode == 0, result.stdout + result.stderr
    marker = installed_project / "launch-completed.txt"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.05)
    assert marker.exists(), "The normal launcher did not start app.py"
    assert str(installed_project / ".venv").lower() in marker.read_text(encoding="utf-8").lower()
