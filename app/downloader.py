"""Fila de downloads com spotDL (um job de cada vez para poupar RAM/CPU).

Um job pode ter vários pedidos (um por linha em `query`), por exemplo todas as faixas de um álbum.
As opções (formato, qualidade, pasta, estrutura) vêm das Configurações.
"""
import asyncio
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from pathlib import Path

import mutagen

from . import dedupe, load, lyrics, scanner, ytsource
from . import settings as app_settings
from . import catalog
from .config import AUDIO_EXTS, DATA_DIR, MUSIC_DIR, SPOTDL_EXTRA_ARGS, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, NICE
from .db import get_conn, row

log = logging.getLogger("downloader")

_wake: asyncio.Event | None = None
_current = {"job_id": None, "proc": None}
_loop = None
# prioridade dos jobs: o que pedes à mão passa à frente das playlists monitorizadas e dos lançamentos automáticos
PRIO_MANUAL, PRIO_AUTO = 0, 5
IDLE_TIMEOUT = int(os.environ.get("NPLAY_DL_IDLE", 600))     # sem nenhuma linha do spotDL durante 10 min = preso → termina e passa à fonte alternativa
ARCHIVE = DATA_DIR / "spotdl-archive.txt"   # URLs do Spotify já descarregados (travão extra contra repetidos)
# Os downloads acontecem numa pasta escondida dentro da pasta da música (o scanner ignora pastas com ".").
# Só depois de verificado (descodifica até ao fim e tem a duração certa) é que o ficheiro entra na biblioteca.
STAGING = MUSIC_DIR / ".nplay-staging"
# cookies do YouTube (opcional): resolvem o "Please sign in" que o YouTube às vezes exige a servidores
COOKIES = DATA_DIR / "youtube-cookies.txt"
ATTEMPTS = 3          # tentativas por job para as músicas que chegarem incompletas

RE_FOUND = re.compile(r"Found (\d+) songs?", re.I)
RE_DONE = re.compile(r'^\s*(Downloaded|Skipping)\b', re.I)
RE_FAIL = re.compile(r"(LookupError|AudioProviderError|DownloaderError|No results found|Failed to download)", re.I)
RE_URL = re.compile(r"(open\.spotify\.com|^spotify:|youtu)", re.I)
MAX_LOG = 20000
AFTER_JOB = []   # funções chamadas depois de cada download (ex.: ligar faixas às playlists sincronizadas)
RE_SPOTIFY_TRACK = re.compile(r"^https://open\.spotify\.com/track/[A-Za-z0-9]+$")


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


_help = {}


def _supports(flag):
    """Algumas opções só existem em versões recentes do spotDL: confirma na ajuda antes de as usar."""
    if "text" not in _help:
        try:
            _help["text"] = subprocess.run(["spotdl", "download", "--help"], capture_output=True, text=True, timeout=60).stdout
        except Exception:  # noqa
            _help["text"] = ""
    return flag in _help["text"]


def build_cmd(queries, s, archive=None):
    fmt = s["dl_format"]
    if fmt == "original":
        # stream original do YouTube (Opus), copiado sem reconverter = melhor qualidade possível
        out_fmt, bitrate = "opus", "disable"
    elif fmt in app_settings.LOSSLESS:
        out_fmt, bitrate = fmt, None
    else:
        out_fmt, bitrate = fmt, s["dl_bitrate"]
    queries = [q.lstrip("-").strip() for q in queries if q.lstrip("-").strip()]   # nada que pareça uma opção
    cmd = [
        *NICE, "spotdl", "download", *queries,
        "--output", app_settings.template(s),
        "--format", out_fmt,
        "--threads", str(s["dl_threads"]),
        "--simple-tui",
        "--overwrite", "skip",
        "--archive", str(archive or ARCHIVE),
    ]
    # o yt-dlp volta a tentar ligações que caem a meio (a causa das músicas "cortadas")
    if _supports("--yt-dlp-args"):
        cmd += ["--yt-dlp-args", "--retries 15 --fragment-retries 15 --retry-sleep 3"]
    if _supports("--max-retries"):
        cmd += ["--max-retries", "5"]
    # se o YouTube Music não tiver (ou bloquear) a música, tenta o YouTube normal
    cmd += ["--audio", "youtube-music", "youtube"]
    if COOKIES.exists() and COOKIES.stat().st_size > 0:
        cmd += ["--cookie-file", str(COOKIES)]
    if bitrate:
        cmd += ["--bitrate", bitrate]
    # as letras já não são procuradas aqui (prioridade 2): ficam para depois, quando a fila de músicas esvaziar
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        cmd += ["--client-id", SPOTIFY_CLIENT_ID, "--client-secret", SPOTIFY_CLIENT_SECRET]
    if SPOTDL_EXTRA_ARGS:
        cmd += shlex.split(SPOTDL_EXTRA_ARGS)
    return cmd


def _insert_job(query: str, label: str | None = None, priority: int = PRIO_MANUAL) -> int:
    conn = get_conn()
    cur = conn.execute("INSERT INTO jobs(query, label, priority) VALUES (?, ?, ?)", (query.strip(), label, priority))
    conn.commit()
    return cur.lastrowid


def _refresh_load():
    n = get_conn().execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
    was = load.busy()
    load.state.update(running=_current["job_id"], queued=n)
    if was and not load.busy():
        load.state["since"] = time.time()


def _put(job_id: int = 0):
    # chamado a partir de threads dos endpoints: o Event asyncio só é seguro dentro do loop
    _refresh_load()
    if _loop is not None and _wake is not None:
        _loop.call_soon_threadsafe(_wake.set)


def enqueue(query: str, label: str | None = None, priority: int = PRIO_MANUAL) -> int:
    jid = _insert_job(query, label, priority)
    _put(jid)
    return jid


REASONS = {"library": "já está na biblioteca", "pending": "já está na fila", "duplicate": "repetida no pedido"}


def enqueue_items(items, label: str | None = None, priority: int = PRIO_MANUAL):
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
        q = str(it.get("query") or "")
        todo.append((key, artist, title, dur, q if RE_SPOTIFY_TRACK.match(q) else f"{artist} - {title}"))
    if not todo:
        return None, 0, skipped
    jid = _insert_job("\n".join(q for *_, q in todo), label, priority)
    conn = get_conn()
    conn.executemany("INSERT INTO dl_items(job_id, key, artist, title, duration) VALUES (?,?,?,?,?)",
                     [(jid, k, a, t, d) for k, a, t, d, _ in todo])
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


# ---------------------------------------------------------------- verificação dos ficheiros
def _probe_duration(path: Path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except Exception:  # noqa
        return None


def _decoded_seconds(path: Path):
    """Descodifica o ficheiro todo e devolve quantos segundos de áudio tem de facto (None = erro).
    Um ficheiro cortado pode manter no cabeçalho a duração original: só a descodificação diz a verdade."""
    try:
        r = subprocess.run([*NICE, "ffmpeg", "-v", "error", "-xerror", "-nostats", "-i", str(path), "-map", "0:a:0",
                            "-f", "null", "-progress", "pipe:1", "-"], capture_output=True, text=True, timeout=600)
    except Exception:  # noqa
        return None
    if r.returncode != 0 or r.stderr.strip():
        return None
    us = [int(l.split("=", 1)[1]) for l in r.stdout.splitlines() if l.startswith("out_time_us=") and l.split("=", 1)[1].strip().lstrip("-").isdigit()]
    return max(us) / 1e6 if us else None


def _tags(path: Path):
    try:
        f = mutagen.File(path, easy=True)
        t = f.tags if f is not None else None
        first = lambda k: str((t.get(k) or [""])[0]) if t else ""  # noqa
        return first("artist").replace(";", ", "), first("title")
    except Exception:  # noqa
        return "", ""


def _expected(job_id, artist, title):
    """Duração esperada: a do pedido (pesquisa no Deezer) ou, para links, a do Deezer."""
    key = dedupe.key_for(artist, title)
    r = get_conn().execute("SELECT duration FROM dl_items WHERE job_id=? AND key=? AND duration IS NOT NULL",
                           (job_id, key)).fetchone()
    if r:
        return r[0]
    try:
        return catalog.lookup_duration(artist, title)
    except Exception:  # noqa
        return None


def verify_file(job_id, path: Path):
    """-> (ok, motivo). Rejeita ficheiros que não descodificam até ao fim ou que ficaram mais curtos."""
    header = _probe_duration(path)
    if not header:
        return False, "não tem áudio legível"
    dur = _decoded_seconds(path)
    if dur is None:
        return False, "está corrompido ou cortado"
    if dur < header * 0.95 - 1.5:
        return False, f"tem {int(dur)} s de áudio de {int(header)} s"
    artist, title = _tags(path)
    exp = _expected(job_id, artist, title) if artist and title else None
    if exp and dur < exp * 0.93 - 2:
        return False, f"tem {int(dur)} s de {int(exp)} s"
    if not exp and dur < 15:
        return False, f"tem só {int(dur)} s"
    return True, ""


def _audio_files(root: Path):
    return [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS]


def _sidecars(p: Path):
    return [x for x in (p.with_suffix(".lrc"), Path(str(p) + ".lrc")) if x.exists()]


def _publish(job_id, stage: Path, dest_root: Path):
    """Move os ficheiros verificados para a biblioteca, mantendo a estrutura Artista/Álbum."""
    lib = dedupe.library_index()
    moved = dupes = 0
    for p in _audio_files(stage):
        rel = p.relative_to(stage)
        dest = dest_root / rel
        artist, title = _tags(p)
        exists = dest.exists() or (artist and title and dedupe.status(title, artist, _probe_duration(p), lib, set()) == "library")
        if exists:                       # já existe na biblioteca: não cria cópias
            for x in [p, *_sidecars(p)]:
                x.unlink(missing_ok=True)
            dupes += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        for x in _sidecars(p):
            shutil.move(str(x), str(dest_root / x.relative_to(stage)))
        shutil.move(str(p), str(dest))
        moved += 1
    return moved, dupes


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
    stage = STAGING / f"job-{job_id}"
    shutil.rmtree(stage, ignore_errors=True)        # restos de uma tentativa interrompida
    stage.mkdir(parents=True, exist_ok=True)
    job_archive = stage / ".archive.txt"
    base_archive = ARCHIVE.read_text() if ARCHIVE.exists() else ""
    _append_log(job_id, f"→ pasta: {workdir}")
    _append_log(job_id, f"→ {len(queries)} pedido(s), formato {s['dl_format']}"
                + ("" if s["dl_format"] in app_settings.LOSSLESS else f" {s['dl_bitrate']}"))
    multi_search = len(queries) > 1
    total = len(queries) if multi_search else 0
    if total:
        _update(job_id, total=total)
    done = failed = 0
    rc = 0
    verified, bad = set(), {}
    for attempt in range(1, ATTEMPTS + 1):
        # o histórico volta ao estado de antes do job: as músicas rejeitadas são descarregadas outra vez,
        # as que já passaram na verificação ficam na pasta e o spotDL salta-as
        job_archive.write_text(base_archive)
        cmd = build_cmd(queries, s, job_archive)
        if attempt > 1:
            _append_log(job_id, f"→ tentativa {attempt} de {ATTEMPTS} para {len(bad)} música(s) incompleta(s)")
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(stage),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,        # grupo próprio: dá para terminar o spotDL e tudo o que ele abriu
        )
        _current.update(job_id=job_id, proc=proc)
        _refresh_load()
        att_done = att_failed = 0
        deadline = time.time() + max(20 * 60, 6 * 60 * max(1, len(queries)))
        stuck = False
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                raw, stuck = None, True
            if raw is not None and not raw:
                break                                   # fim normal da saída
            if stuck or time.time() > deadline:
                _append_log(job_id, "✗ o spotDL deixou de responder — terminado; passa à fonte alternativa")
                _kill(proc)
                stuck = True
                break
            line = raw.decode(errors="replace").rstrip()
            if not line.strip():
                continue
            _append_log(job_id, line)
            if not multi_search and (m := RE_FOUND.search(line)):
                total = max(total, int(m.group(1)))
            elif RE_DONE.search(line):
                att_done += 1
            elif RE_FAIL.search(line):
                att_failed += 1
            if att_done > done or attempt == 1:
                done, failed = max(done, att_done), att_failed
                _update(job_id, done=done, failed=failed, total=max(total, done + failed))
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            # o processo já fechou a saída mas o fim não foi assinalado (aconteceu: ficou 32 h "a descarregar")
            _kill(proc)
            rc = proc.returncode if proc.returncode is not None else 0
            _append_log(job_id, "→ o spotDL terminou sem confirmar; a continuar")
        if stuck:
            rc = 0
        _current["proc"] = None
        killed = rc < 0 or rc in (137, 143)
        if killed:
            _append_log(job_id, "✗ o spotDL foi interrompido (falta de memória no servidor?)"
                        + (" — nova tentativa daqui a 20 s" if attempt < ATTEMPTS else ""))
        if (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") == "cancelled":
            break
        # verificação de cada ficheiro novo: só passa o que está completo
        bad = {}
        for p in _audio_files(stage):
            rel = str(p.relative_to(stage))
            if rel in verified:
                continue
            ok, why = await asyncio.to_thread(verify_file, job_id, p)
            if ok:
                verified.add(rel)
            else:
                bad[rel] = why
                _append_log(job_id, f"✗ {rel}: {why} — apagado")
                for x in [p, *_sidecars(p)]:
                    x.unlink(missing_ok=True)
        if killed and attempt < ATTEMPTS and (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") != "cancelled":
            await asyncio.sleep(20)
            continue
        if not bad:
            break
    cancelled = (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") == "cancelled"
    # fonte alternativa (yt-dlp direto) para o que o spotDL não conseguiu
    if not cancelled:
        recovered = await _fallback(job_id, queries, stage, s, verified)
        if recovered:
            done += recovered
            failed = max(0, failed - recovered)
            bad = {k: v for k, v in bad.items() if (stage / k).exists()}
            rc = 0
        cancelled = (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") == "cancelled"
    if cancelled:
        shutil.rmtree(stage, ignore_errors=True)
        status = "cancelled"
    else:
        moved, dupes = await asyncio.to_thread(_publish, job_id, stage, workdir)
        if not bad and job_archive.exists():      # só guarda no histórico quando tudo chegou completo
            extra = [l for l in job_archive.read_text().splitlines() if l.strip() and l not in set(base_archive.splitlines())]
            if extra:
                with ARCHIVE.open("a") as f:
                    f.write("\n".join(extra) + "\n")
        shutil.rmtree(stage, ignore_errors=True)
        _append_log(job_id, f"✓ {moved} música(s) verificada(s) e organizada(s)"
                    + (f" · {dupes} já existia(m)" if dupes else "")
                    + (f" · {len(bad)} sem versão completa" if bad else ""))
        if bad:
            failed = max(failed, len(bad))
            done = max(0, done - len(bad))
        if rc == 0 and (done > 0 or failed == 0 or moved):
            status = "done" if failed == 0 else "partial"
        else:
            status = "failed"
    if status in ("done", "partial") and multi_search and total > done + failed:
        done = total - failed          # as restantes já existiam (o spotDL ignorou-as) — contam como concluídas
    _update(job_id, status=status, done=done, failed=failed, finished_at=int(time.time()))
    # incorpora as faixas novas na biblioteca, fecha os registos e procura letras em falta
    await asyncio.to_thread(scanner.scan)
    _finalize_items(job_id, status)
    for fn in AFTER_JOB:
        try:
            await asyncio.to_thread(fn, job_id)
        except Exception:  # noqa
            log.exception("hook pós-download falhou")
    asyncio.get_running_loop().run_in_executor(None, lyrics.prefetch, 60)


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _wanted(job_id, queries):
    """Músicas que o job devia trazer: [(artist, title, duration)]."""
    items = get_conn().execute("SELECT artist, title, duration FROM dl_items WHERE job_id=?", (job_id,)).fetchall()
    if items:
        return [(r[0], r[1], r[2]) for r in items]
    out = []
    for q in queries:
        if RE_URL.search(q) or " - " not in q:
            continue                            # links: o spotDL é que sabe o que contêm
        a, t = q.split(" - ", 1)
        out.append((a.strip(), t.strip(), None))
    return out


async def _fallback(job_id, queries, stage, s, verified):
    """Procura no YouTube Music / YouTube / SoundCloud as músicas que ainda faltam. -> nº recuperadas."""
    wanted = _wanted(job_id, queries)
    if not wanted:
        return 0
    have = set()
    for p in _audio_files(stage):
        a, t = _tags(p)
        if a and t:
            have.add(dedupe.key_for(a.split(",")[0], t))
            have.add(dedupe.key_for(a, t))
    lib = dedupe.library_index()
    missing = [(a, t, d) for a, t, d in wanted
               if dedupe.key_for(a, t) not in have and dedupe.key_for(a.split(",")[0], t) not in have
               and dedupe.status(t, a, d, lib, set()) != "library"]
    if not missing:
        return 0
    _append_log(job_id, f"→ fonte alternativa para {len(missing)} música(s) em falta")
    got = 0
    for a, t, d in missing:
        if (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") == "cancelled":
            break
        path = await asyncio.to_thread(ytsource.fetch, a, t, d, stage, s, lambda l: _append_log(job_id, l))
        if not path:
            _append_log(job_id, f"✗ {a} - {t}: nenhuma fonte tinha uma versão completa")
            continue
        ok, why = await asyncio.to_thread(verify_file, job_id, path)
        if ok:
            verified.add(str(path.relative_to(stage)))
            got += 1
        else:
            _append_log(job_id, f"✗ {path.name}: {why} — apagado")
            path.unlink(missing_ok=True)
    return got


def _next_job():
    r = get_conn().execute("SELECT id FROM jobs WHERE status='queued' ORDER BY COALESCE(priority, 0), id LIMIT 1").fetchone()
    return r[0] if r else None


async def worker():
    global _loop, _wake
    _loop = asyncio.get_running_loop()
    _wake = asyncio.Event()
    conn = get_conn()
    shutil.rmtree(STAGING, ignore_errors=True)    # downloads a meio de um reinício nunca chegam à biblioteca
    # retoma jobs interrompidos por um reinício
    conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
    conn.commit()
    load.state["since"] = time.time()
    while True:
        _refresh_load()
        job_id = _next_job()
        if job_id is None:
            _wake.clear()
            try:
                await asyncio.wait_for(_wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            continue
        _current["job_id"] = job_id
        _refresh_load()
        try:
            await _run_job(job_id)
        except Exception as e:  # noqa
            log.exception("Job %s falhou", job_id)
            _append_log(job_id, f"Erro: {e}")
            _update(job_id, status="failed", finished_at=int(time.time()))
            _finalize_items(job_id, "failed")
        finally:
            _current.update(job_id=None, proc=None)
            if (row("SELECT status FROM jobs WHERE id=?", (job_id,)) or {}).get("status") in ("queued", "running"):
                _update(job_id, status="failed", finished_at=int(time.time()))
            _refresh_load()


def cancel(job_id: int):
    _update(job_id, status="cancelled", finished_at=int(time.time()))
    _finalize_items(job_id, "cancelled")
    if _current["job_id"] == job_id and _current["proc"]:
        _kill(_current["proc"])
    _refresh_load()
