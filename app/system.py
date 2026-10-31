"""Informação do sistema e manutenção (versões, disco, atualização do yt-dlp)."""
import shutil
import subprocess
from pathlib import Path

from .config import MUSIC_DIR

VENV = Path("/opt/spotdl")  # ambiente isolado criado pelo Dockerfile
_versions = {}


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or r.stderr or "").strip()
        return out or None
    except Exception:
        return None


def versions(refresh=False):
    if _versions and not refresh:
        return dict(_versions)
    sp = _run(["spotdl", "--version"], 60) if shutil.which("spotdl") else None
    py = VENV / "bin" / "python"
    if py.exists():
        yt = _run([str(py), "-c", "import yt_dlp.version as v;print(v.__version__)"])
    elif shutil.which("yt-dlp"):
        yt = _run(["yt-dlp", "--version"])
    else:
        yt = None
    _versions.update(spotdl=(sp.splitlines()[-1] if sp else None), ytdlp=yt)
    return dict(_versions)


def disk():
    try:
        du = shutil.disk_usage(MUSIC_DIR)
        return {"total": du.total, "used": du.used, "free": du.free}
    except OSError:
        return None


def update_ytdlp():
    pip = VENV / "bin" / "pip"
    if not pip.exists():
        raise RuntimeError("O ambiente isolado do spotDL não existe (só está disponível na imagem Docker).")
    r = subprocess.run([str(pip), "install", "-U", "yt-dlp"], capture_output=True, text=True, timeout=240)
    versions(refresh=True)
    return {"ok": r.returncode == 0, "log": ((r.stdout or "") + (r.stderr or ""))[-1500:], **_versions}
