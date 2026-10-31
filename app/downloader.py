"""Fila de downloads com spotDL (um job de cada vez para poupar RAM/CPU)."""
import asyncio
import logging
import re
import shlex
import shutil
import time

from . import scanner
from .config import (MUSIC_DIR, SPOTDL_BITRATE, SPOTDL_EXTRA_ARGS, SPOTDL_FORMAT, SPOTDL_OUTPUT,
                     SPOTDL_THREADS, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)
from .db import get_conn, row

log = logging.getLogger("downloader")

queue: "asyncio.Queue[int]" = asyncio.Queue()
_current = {"job_id": None, "proc": None}

RE_FOUND = re.compile(r"Found (\d+) songs?", re.I)
RE_DONE = re.compile(r'^\s*(Downloaded|Skipping)\b', re.I)
RE_FAIL = re.compile(r"(LookupError|AudioProviderError|DownloaderError|No results found|Failed to download)", re.I)
MAX_LOG = 20000


def spotdl_available():
    return shutil.which("spotdl") is not None


def _update(job_id, **fields):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))
    conn.commit()


def _append_log(job_id, line):
    conn = get_conn()
    conn.execute(
        "UPDATE jobs SET log = substr(COALESCE(log,'') || ? || char(10), -?) WHERE id=?",
        (line, MAX_LOG, job_id),
    )
    conn.commit()


def build_cmd(query: str):
    cmd = [
        "spotdl", "download", query,
        "--output", SPOTDL_OUTPUT,
        "--format", SPOTDL_FORMAT,
        "--bitrate", SPOTDL_BITRATE,
        "--threads", str(SPOTDL_THREADS),
        "--simple-tui",
        "--overwrite", "skip",
    ]
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        cmd += ["--client-id", SPOTIFY_CLIENT_ID, "--client-secret", SPOTIFY_CLIENT_SECRET]
    if SPOTDL_EXTRA_ARGS:
        cmd += shlex.split(SPOTDL_EXTRA_ARGS)
    return cmd


def enqueue(query: str) -> int:
    conn = get_conn()
    cur = conn.execute("INSERT INTO jobs(query) VALUES (?)", (query.strip(),))
    conn.commit()
    queue.put_nowait(cur.lastrowid)
    return cur.lastrowid


async def _run_job(job_id: int):
    job = row("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] == "cancelled":
        return
    _update(job_id, status="running", log="", done=0, failed=0, total=0)
    if not spotdl_available():
        _append_log(job_id, "spotdl não está instalado no container.")
        _update(job_id, status="failed", finished_at=int(time.time()))
        return
    cmd = build_cmd(job["query"])
    _append_log(job_id, "$ " + " ".join(shlex.quote(c) for c in cmd[:3]) + " …")
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(MUSIC_DIR),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    _current.update(job_id=job_id, proc=proc)
    done = failed = total = 0
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line.strip():
            continue
        _append_log(job_id, line)
        if m := RE_FOUND.search(line):
            total = int(m.group(1))
        elif RE_DONE.search(line):
            done += 1
        elif RE_FAIL.search(line):
            failed += 1
        _update(job_id, done=done, failed=failed, total=max(total, done + failed))
    rc = await proc.wait()
    _current.update(job_id=None, proc=None)
    current = row("SELECT status FROM jobs WHERE id=?", (job_id,))
    if current and current["status"] == "cancelled":
        status = "cancelled"
    elif rc == 0 and (done > 0 or failed == 0):
        status = "done" if failed == 0 else "partial"
    else:
        status = "failed"
    _update(job_id, status=status, finished_at=int(time.time()))
    # incorpora as faixas novas na biblioteca
    await asyncio.to_thread(scanner.scan)


async def worker():
    conn = get_conn()
    # retoma jobs interrompidos por um reinício
    for r in conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running') ORDER BY id").fetchall():
        conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (r[0],))
        queue.put_nowait(r[0])
    conn.commit()
    while True:
        job_id = await queue.get()
        try:
            await _run_job(job_id)
        except Exception as e:  # noqa
            log.exception("Job %s falhou", job_id)
            _append_log(job_id, f"Erro: {e}")
            _update(job_id, status="failed", finished_at=int(time.time()))
        finally:
            queue.task_done()


def cancel(job_id: int):
    _update(job_id, status="cancelled", finished_at=int(time.time()))
    if _current["job_id"] == job_id and _current["proc"]:
        try:
            _current["proc"].terminate()
        except ProcessLookupError:
            pass
