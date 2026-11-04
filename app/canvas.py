"""Vídeos curtos (estilo Canvas do Spotify) gerados automaticamente.

O Spotify não disponibiliza os Canvas. Em alternativa, para cada música:
  1. procura o videoclipe oficial no YouTube (yt-dlp, o mesmo que o spotDL usa);
  2. descarrega só ~9 segundos a meio do vídeo, sem áudio, em baixa resolução;
  3. descarta vídeos parados (lyric videos, capas estáticas) com o filtro freezedetect do ffmpeg;
  4. converte para vertical 9:16, 8 s, sem som, pronto para loop.
Um vídeo posto à mão (canvas.mp4 na pasta do álbum ou Título.canvas.mp4) tem sempre prioridade.
"""
import logging
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .config import DATA_DIR, MUSIC_DIR
from .db import get_conn, row

log = logging.getLogger("canvas")
CANVAS_DIR = DATA_DIR / "canvas"
CANVAS_DIR.mkdir(parents=True, exist_ok=True)
VENV_PY = Path("/opt/spotdl/bin/python")
RETRY_AFTER = 30 * 86400
CLIP_START = 45      # segundos a partir do início do videoclipe
CLIP_LEN = 9

_q: "queue.PriorityQueue[tuple[int, float, int]]" = queue.PriorityQueue()
_queued = set()
state = {"running": False, "current": None, "done": 0, "made": 0}


def generated_path(track_id: int) -> Path:
    return CANVAS_DIR / f"{track_id}.mp4"


def manual_file(track_path: Path):
    for ext in (".mp4", ".webm"):
        for p in (Path(str(track_path.with_suffix("")) + ".canvas" + ext), track_path.parent / ("canvas" + ext)):
            if p.exists():
                return p
    return None


def find(track_id: int, track_path: Path):
    return manual_file(track_path) or (generated_path(track_id) if generated_path(track_id).exists() else None)


def status(track_id: int):
    r = row("SELECT status, tried_at FROM canvas WHERE track_id=?", (track_id,))
    return r


def should_try(track_id: int):
    r = status(track_id)
    if not r:
        return True
    return r["status"] == "error" and time.time() - r["tried_at"] > 86400 or (r["status"] == "none" and time.time() - r["tried_at"] > RETRY_AFTER)


def request(track_id: int, priority: int = 5):
    """Pede um vídeo para a faixa (0 = a tocar agora, 5 = downloads novos, 9 = biblioteca toda)."""
    if track_id in _queued or not should_try(track_id):
        return False
    _queued.add(track_id)
    _q.put((priority, time.time(), track_id))
    return True


def _ytdlp():
    py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
    return [str(py), "-m", "yt_dlp"]


def _fetch_clip(artist: str, title: str, out_dir: Path):
    """Descarrega ~9 s do videoclipe oficial (só vídeo). Devolve o caminho ou None."""
    q = f"ytsearch4:{artist} {title} official music video"
    cmd = _ytdlp() + [
        q, "--match-filter", "duration > 90 & duration < 900 & !is_live",
        "--max-downloads", "1", "--no-playlist", "--quiet", "--no-warnings", "--no-part",
        "-f", "bv*[height<=720][ext=mp4]/bv*[height<=720]/bv*",
        "--download-sections", f"*{CLIP_START}-{CLIP_START + CLIP_LEN}",
        "-o", str(out_dir / "clip.%(ext)s"),
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=240)  # --max-downloads sai com código 101
    files = [p for p in out_dir.glob("clip.*") if p.stat().st_size > 10_000]
    return files[0] if files else None


def _is_static(clip: Path):
    """True se o vídeo está quase todo parado (lyric video, capa fixa…)."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(clip), "-t", str(CLIP_LEN), "-an",
                        "-vf", "freezedetect=n=0.003:d=2", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=120)
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", r.stderr))
    open_start = re.findall(r"freeze_start:\s*([\d.]+)", r.stderr)
    ends = re.findall(r"freeze_end", r.stderr)
    if len(open_start) > len(ends):  # parado até ao fim do excerto
        frozen += CLIP_LEN - float(open_start[-1])
    return frozen >= CLIP_LEN * 0.55


def _encode(clip: Path, out: Path):
    tmp = out.with_suffix(".tmp.mp4")
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.3", "-i", str(clip), "-t", "8", "-an",
                    "-vf", "scale=540:960:force_original_aspect_ratio=increase,crop=540:960,fps=24,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-movflags", "+faststart", str(tmp)],
                   check=True, capture_output=True, timeout=180)
    tmp.replace(out)


def _save(track_id, st):
    conn = get_conn()
    conn.execute("INSERT INTO canvas(track_id, status, tried_at) VALUES (?,?,?) ON CONFLICT(track_id) "
                 "DO UPDATE SET status=excluded.status, tried_at=excluded.tried_at", (track_id, st, int(time.time())))
    conn.commit()


def make(track_id: int):
    t = row("SELECT path, title, artist FROM tracks WHERE id=?", (track_id,))
    if not t:
        return "none"
    if manual_file(MUSIC_DIR / t["path"]) or generated_path(track_id).exists():
        _save(track_id, "ok")
        return "ok"
    title = re.sub(r"\s*[\(\[](feat|ft|with)\.?[^\)\]]*[\)\]]", "", t["title"], flags=re.I).strip()
    artist = t["artist"].split(",")[0].strip()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            clip = _fetch_clip(artist, title, Path(tmp))
            if not clip:
                _save(track_id, "none")
                return "none"
            if _is_static(clip):
                _save(track_id, "none")
                return "none"
            _encode(clip, generated_path(track_id))
            _save(track_id, "ok")
            return "ok"
        except Exception as e:  # noqa
            log.info("canvas %s falhou: %s", track_id, e)
            _save(track_id, "error")
            return "error"


def _worker():
    while True:
        _p, _t, tid = _q.get()
        state.update(running=True, current=tid)
        try:
            if make(tid) == "ok":
                state["made"] += 1
        except Exception:  # noqa
            log.exception("canvas")
        finally:
            state["done"] += 1
            _queued.discard(tid)
            state.update(running=not _q.empty(), current=None)
            _q.task_done()


def start():
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg não encontrado: vídeos curtos desativados")
        return
    threading.Thread(target=_worker, daemon=True).start()


def request_all():
    n = 0
    for r in get_conn().execute("SELECT t.id FROM tracks t LEFT JOIN canvas c ON c.track_id=t.id "
                                "WHERE c.track_id IS NULL ORDER BY t.added_at DESC"):
        n += request(r[0], 9)
    return n


def stats():
    r = row("SELECT (SELECT COUNT(*) FROM tracks) AS tracks, "
            "(SELECT COUNT(*) FROM canvas WHERE status='ok') AS ok, "
            "(SELECT COUNT(*) FROM canvas WHERE status='none') AS none")
    return {**r, "queued": _q.qsize(), **state}
