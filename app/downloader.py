"""Fila de downloads com spotDL (um job de cada vez para poupar RAM/CPU).

Um job pode ter vários pedidos (um por linha em `query`), por exemplo todas as faixas de um álbum.
As opções (formato, qualidade, pasta, estrutura) vêm das Configurações.
"""
import asyncio
import logging
import re
import shlex
import shutil
import time

from . import dedupe, lyrics, scanner
from . import settings as app_settings
from .config import DATA_DIR, SPOTDL_EXTRA_ARGS, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from .db import get_conn, row

log = logging.getLogger("downloader")

queue: "asyncio.Queue[int]" = asyncio.Queue()
_current = {"job_id": None, "proc": None}
_loop = None
ARCHIVE = DATA_DIR / "spotdl-archive.txt"   # URLs do Spotify já descarregados (travão extra contra repetidos)

RE_FOUND = re.compile(r"Found (\d+) songs?", re.I)
RE_DONE = re.compile(r'^\s*(Downloaded|Skipping)\b', re.I)
RE_FAIL = re.compile(r"(LookupError|AudioProviderError|DownloaderError|No results found|Failed to download)", re.I)
RE_URL = re.compile(r"(open\.spotify\.com|^spotify:|youtu)", re.I)
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


def build_cmd(queries, s):
    fmt = s["dl_format"]
    if fmt == "original":
        # stream original do YouTube (Opus), copiado sem reconverter = melhor qualidade possível
        out_fmt, bitrate = "opus", "disable"
    elif fmt in app_settings.LOSSLESS:
        out_fmt, bitrate = fmt, None
    else:
        out_fmt, bitrate = fmt, s["dl_bitrate"]
    cmd = [
        "spotdl", "download", *queries,
        "--output", app_settings.template(s),
        "--format", out_fmt,
        "--threads", str(s["dl_threads"]),
        "--simple-tui",
        "--overwrite", "skip",
        "--archive", str(ARCHIVE),
    ]
    if bitrate:
        cmd += ["--bitrate", bitrate]
    if s["dl_lyrics"]:
        # letra sincronizada primeiro; --generate-lrc guarda também um .lrc ao lado da música
        cmd += ["--lyrics", "synced", "genius", "musixmatch", "azlyrics", "--generate-lrc"]
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        cmd += ["--client-id", SPOTIFY_CLIENT_ID, "--client-secret", SPOTIFY_CLIENT_SECRET]
    if SPOTDL_EXTRA_ARGS:
        cmd += shlex.split(SPOTDL_EXTRA_ARGS)
    return cmd


def _insert_job(query: str, label: str | None = None) -> int:
    conn = get_conn()
    cur = conn.execute("INSERT INTO jobs(query, label) VALUES (?, ?)", (query.strip(), label))
    conn.commit()
    return cur.lastrowid


def _put(job_id: int):
    # chamado a partir de threads dos endpoints: a fila asyncio só é segura dentro do loop
    if _loop is not None:
        _loop.call_soon_threadsafe(queue.put_nowait, job_id)
    else:
        queue.put_nowait(job_id)


def enqueue(query: str, label: str | None = None) -> int:
    jid = _insert_job(query, label)
    _put(jid)
    return jid


REASONS = {"library": "já está na biblioteca", "pending": "já está na fila", "duplicate": "repetida no pedido"}


def enqueue_items(items, label: str | None = None):
    """Enfileira músicas [{artist, title, duration}] ignorando as que já existem. -> (job_id|None, queued, skipped)"""
    lib, pend = dedupe.library_index(), dedupe.pending_keys()
    todo, skipped, seen = [], [], set()
    for it in items[:200]:
        artist, title = str(it.get("artist") or "").strip(), str(it.get("title") or "").strip()
        if not artist or not title:
            continue
        key = dedupe.key_for(artist, title)
        dur = it.get("duration") if isinstance(it.get("duration"), (int, float)) else None
        st = "duplicate" if key in seen else dedupe.status(title, artist, dur, lib, pend)
        if st:
            skipped.append({"title": title, "artist": artist, "reason": REASONS[st], "why": st})
            continue
        seen.add(key)
        todo.append((key, artist, title, dur))
    if not todo:
        return None, 0, skipped
    jid = _insert_job("\n".join(f"{a} - {t}" for _, a, t, _ in todo), label)
    conn = get_conn()
    conn.executemany("INSERT INTO dl_items(job_id, key, artist, title, duration) VALUES (?,?,?,?,?)",
                     [(jid, k, a, t, d) for k, a, t, d in todo])
    conn.commit()
    _put(jid)
    return jid, len(todo), skipped


def _finalize_items(job_id: int, job_status: str):
    conn = get_conn()
    items = conn.execute("SELECT id, artist, title, duration FROM dl_items WHERE job_id=? AND status IN ('queued','running')",
                         (job_id,)).fetchall()
    if not items:
        return
    if job_status == "cancelled":
        conn.execute("UPDATE dl_items SET status='cancelled' WHERE job_id=? AND status IN ('queued','running')", (job_id,))
    else:
        lib = dedupe.library_index()
        for it in items:
            ok = dedupe.status(it["title"], it["artist"], it["duration"], lib, set()) == "library"
            conn.execute("UPDATE dl_items SET status=? WHERE id=?", ("done" if ok else "failed", it["id"]))
    conn.commit()


def _split(query: str):
    # um "-" no início seria lido como opção pelo spotDL
    return [q.strip().lstrip("-").strip() for q in query.split("\n") if q.strip().lstrip("-").strip()]


async def _run_job(job_id: int):
    job = row("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not job or job["status"] == "cancelled":
        return
    queries = _split(job["query"])
    _update(job_id, status="running", log="", done=0, failed=0, total=0)
    get_conn().execute("UPDATE dl_items SET status='running' WHERE job_id=? AND status='queued'", (job_id,))
    get_conn().commit()
    if not spotdl_available():
        _append_log(job_id, "spotdl não está instalado no container.")
        _update(job_id, status="failed", finished_at=int(time.time()))
        _finalize_items(job_id, "failed")
        return
    s = app_settings.get_all()
    workdir = app_settings.download_path(s)
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(queries, s)
    _append_log(job_id, f"→ pasta: {workdir}")
    _append_log(job_id, f"→ {len(queries)} pedido(s), formato {s['dl_format']}"
                + ("" if s["dl_format"] in app_settings.LOSSLESS else f" {s['dl_bitrate']}"))
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    _current.update(job_id=job_id, proc=proc)
    multi_search = len(queries) > 1 and not any(RE_URL.search(q) for q in queries)
    total = len(queries) if multi_search else 0
    if total:
        _update(job_id, total=total)
    done = failed = 0
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line.strip():
            continue
        _append_log(job_id, line)
        if not multi_search and (m := RE_FOUND.search(line)):
            total = max(total, int(m.group(1)))
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
    if status in ("done", "partial") and multi_search and total > done + failed:
        done = total - failed          # as restantes já existiam (o spotDL ignorou-as) — contam como concluídas
    _update(job_id, status=status, done=done, finished_at=int(time.time()))
    # incorpora as faixas novas na biblioteca, fecha os registos e procura letras em falta
    await asyncio.to_thread(scanner.scan)
    _finalize_items(job_id, status)
    asyncio.get_running_loop().run_in_executor(None, lyrics.prefetch, 60)


async def worker():
    global _loop
    _loop = asyncio.get_running_loop()
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
    _finalize_items(job_id, "cancelled")
    if _current["job_id"] == job_id and _current["proc"]:
        try:
            _current["proc"].terminate()
        except ProcessLookupError:
            pass
