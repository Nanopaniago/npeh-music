"""Playlists sincronizadas: um link de playlist (Spotify ou Deezer) cria uma playlist no Pobrefy
que é verificada de X em X horas. Músicas novas na fonte são descarregadas (sem repetidos) e a
playlist local fica sempre com a mesma ordem que a original.
"""
import json
import logging
import re
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import dedupe, downloader
from . import settings as app_settings
from .config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from .db import get_conn, row, rows

log = logging.getLogger("sources")
RE_SPOTIFY = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?playlist/|spotify:playlist:)([A-Za-z0-9]{10,})")
RE_DEEZER = re.compile(r"deezer\.com/(?:[a-z]{2}/)?playlist/(\d+)")
RE_DEEZER_SHORT = re.compile(r"(link\.deezer\.com|deezer\.page\.link)/", re.I)
RE_SPOTIFY_TRACK = re.compile(r"^https://open\.spotify\.com/track/[A-Za-z0-9]+")
_lock = threading.Lock()
syncing = set()


def parse_url(url: str):
    """-> (kind, url canónico) ou None se não for um link de playlist suportado."""
    url = (url or "").strip()
    if RE_DEEZER_SHORT.search(url):
        try:  # links curtos do Deezer: segue o redirecionamento
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Pobrefy/1.0"}), timeout=10) as r:
                url = r.geturl()
        except Exception:  # noqa
            return None
    if m := RE_SPOTIFY.search(url):
        return "spotify", f"https://open.spotify.com/playlist/{m.group(1)}"
    if m := RE_DEEZER.search(url):
        return "deezer", f"https://www.deezer.com/playlist/{m.group(1)}"
    return None


# ---------------------------------------------------------------- leitura da fonte
def _spotify_save(url):
    """`spotdl save`: lê a playlist inteira sem descarregar nada (pode ser lento com o limite da API do Spotify)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "pl.spotdl"
        cmd = ["spotdl", "save", url, "--save-file", str(out), "--lyrics", "--threads", "4"]
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            cmd += ["--client-id", SPOTIFY_CLIENT_ID, "--client-secret", SPOTIFY_CLIENT_SECRET]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=tmp)
        if not out.exists():
            tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
            raise RuntimeError(" ".join(tail)[-300:] or "o spotDL não devolveu músicas")
        songs = json.loads(out.read_text(encoding="utf-8"))
    name = next((s.get("list_name") for s in songs if s.get("list_name")), None)
    items = [{"title": s.get("name") or "", "artist": ", ".join(s.get("artists") or []) or s.get("artist") or "",
              "duration": s.get("duration"), "query": s.get("url") or ""} for s in songs if s.get("name")]
    return name, items


RE_SP_ID = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")


def _spotify_embed(url):
    """Leitura rápida pela página pública de incorporação do Spotify (até 100 músicas, sem limites da API)."""
    m = RE_SP_ID.search(url)
    if not m:
        raise RuntimeError("link do Spotify inválido")
    req = urllib.request.Request(f"https://open.spotify.com/embed/playlist/{m.group(1)}",
                                 headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    j = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not j:
        raise RuntimeError("a página do Spotify mudou")
    ent = json.loads(j.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    items = []
    for t in ent.get("trackList") or []:
        tid = (t.get("uri") or "").split(":")[-1]
        if not t.get("title") or t.get("entityType", "track") != "track":
            continue
        items.append({"title": t["title"], "artist": t.get("subtitle") or "",
                      "duration": round((t.get("duration") or 0) / 1000) or None,
                      "query": f"https://open.spotify.com/track/{tid}" if tid else ""})
    return ent.get("name") or ent.get("title"), items


def _fetch_spotify(url):
    """Primeiro a leitura rápida (embed); se a playlist tiver mais de 100 músicas, completa com o spotDL."""
    name, items = None, []
    try:
        name, items = _spotify_embed(url)
    except Exception as e:  # noqa
        log.info("embed do Spotify falhou (%s); a usar o spotDL", e)
    if len(items) >= 100 or not items:
        try:
            n2, full = _spotify_save(url)
            if len(full) > len(items):
                name, items = n2 or name, full
        except Exception as e:  # noqa
            if not items:
                raise
            log.warning("spotDL save falhou (%s); a usar as primeiras %s músicas", e, len(items))
    if not items:
        raise RuntimeError("o Spotify não devolveu músicas")
    return name, None, items


def _deezer(path, params=""):
    req = urllib.request.Request(f"https://api.deezer.com{path}{params}", headers={"User-Agent": "Pobrefy/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.load(r)
    if isinstance(d, dict) and "error" in d:
        raise RuntimeError(d["error"].get("message", "erro do Deezer") if isinstance(d["error"], dict) else str(d["error"]))
    return d


def _fetch_deezer(url):
    pid = RE_DEEZER.search(url).group(1)
    meta = _deezer(f"/playlist/{pid}")
    items, index = [], 0
    while True:
        page = _deezer(f"/playlist/{pid}/tracks", f"?limit=100&index={index}")
        data = page.get("data") or []
        for t in data:
            if t.get("readable") is False:
                continue
            items.append({"title": t.get("title") or "", "artist": (t.get("artist") or {}).get("name", ""),
                          "duration": t.get("duration"), "query": ""})
        index += len(data)
        if not data or not page.get("next") or index >= 2000:
            break
    return meta.get("title"), meta.get("picture_big") or meta.get("picture_medium"), items


def fetch(kind, url):
    return _fetch_spotify(url) if kind == "spotify" else _fetch_deezer(url)


# ---------------------------------------------------------------- ligação às faixas da biblioteca
def _library_map():
    idx = {}
    for r in get_conn().execute("SELECT id, title, artist, duration FROM tracks ORDER BY added_at"):
        idx.setdefault(dedupe.key_for(r["artist"], r["title"]), []).append((r["id"], r["duration"]))
    return idx


def relink(source_id=None):
    """Reconstrói cada playlist sincronizada com a ordem da fonte, usando as faixas que já existem."""
    lib = _library_map()
    conn = get_conn()
    srcs = rows("SELECT * FROM pl_sources" + (" WHERE id=?" if source_id else ""), (source_id,) if source_id else ())
    for s in srcs:
        wanted, seen = [], set()
        for it in conn.execute("SELECT key, duration FROM pl_source_items WHERE source_id=? ORDER BY position", (s["id"],)):
            for tid, dur in lib.get(it["key"], []):
                if dedupe._close(it["duration"], dur) and tid not in seen:
                    wanted.append(tid)
                    seen.add(tid)
                    break
        cur_rows = conn.execute("SELECT track_id, COALESCE(manual,0) FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                                (s["playlist_id"],)).fetchall()
        current = [r[0] for r in cur_rows]
        # as músicas que adicionaste à mão ficam, no fim, pela ordem em que as puseste
        manual = [r[0] for r in cur_rows if r[1] and r[0] not in seen]
        final = wanted + list(dict.fromkeys(manual))
        if current != final:
            conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=?", (s["playlist_id"],))
            conn.executemany("INSERT INTO playlist_tracks(playlist_id, track_id, position, manual) VALUES (?,?,?,?)",
                             [(s["playlist_id"], tid, i, 1 if i > len(wanted) else 0) for i, tid in enumerate(final, 1)])
            conn.execute("UPDATE playlists SET updated_at=strftime('%s','now') WHERE id=?", (s["playlist_id"],))
        conn.execute("UPDATE pl_sources SET matched=? WHERE id=?", (len(wanted), s["id"]))
    conn.commit()


# ---------------------------------------------------------------- importar / sincronizar
def import_url(url):
    parsed = parse_url(url)
    if not parsed:
        raise ValueError("Link de playlist não reconhecido (aceito playlists do Spotify e do Deezer).")
    kind, canon = parsed
    existing = row("SELECT * FROM pl_sources WHERE url=?", (canon,))
    if existing:
        start_sync(existing["id"])
        return existing["playlist_id"], False
    conn = get_conn()
    pid = conn.execute("INSERT INTO playlists(name) VALUES (?)", ("A importar playlist…",)).lastrowid
    sid = conn.execute("INSERT INTO pl_sources(playlist_id, url, kind, last_status) VALUES (?,?,?, 'syncing')",
                       (pid, canon, kind)).lastrowid
    conn.commit()
    start_sync(sid)
    return pid, True


def start_sync(source_id):
    if source_id in syncing:
        return
    syncing.add(source_id)
    get_conn().execute("UPDATE pl_sources SET last_status='syncing' WHERE id=?", (source_id,))
    get_conn().commit()
    threading.Thread(target=sync, args=(source_id,), daemon=True).start()


def sync(source_id):
    try:
        with _lock:  # uma sincronização de cada vez (poupa RAM no servidor)
            s = row("SELECT * FROM pl_sources WHERE id=?", (source_id,))
            if not s:
                return
            conn = get_conn()
            try:
                name, cover, items = fetch(s["kind"], s["url"])
            except Exception as e:  # noqa
                log.warning("sync %s falhou: %s", s["url"], e)
                conn.execute("UPDATE pl_sources SET last_status='error', last_error=?, last_sync=? WHERE id=?",
                             (str(e)[:300], int(time.time()), source_id))
                conn.commit()
                return
            conn.execute("DELETE FROM pl_source_items WHERE source_id=?", (source_id,))
            conn.executemany(
                "INSERT INTO pl_source_items(source_id, position, key, title, artist, duration, query) VALUES (?,?,?,?,?,?,?)",
                [(source_id, i, dedupe.key_for(it["artist"], it["title"]), it["title"], it["artist"], it["duration"], it["query"])
                 for i, it in enumerate(items, 1)])
            if name:
                conn.execute("UPDATE playlists SET name=? WHERE id=?", (name[:120], s["playlist_id"]))
            conn.execute("UPDATE pl_sources SET name=COALESCE(?, name), cover=COALESCE(?, cover), total=?, last_sync=?, "
                         "last_status='ok', last_error=NULL WHERE id=?",
                         (name, cover, len(items), int(time.time()), source_id))
            conn.commit()
            relink(source_id)
            # descarrega só o que falta (o downloader ignora o que já existe ou já está na fila)
            todo = [{"artist": it["artist"], "title": it["title"], "duration": it["duration"],
                     "query": it["query"] if RE_SPOTIFY_TRACK.match(it["query"] or "") else None} for it in items]
            for i in range(0, len(todo), 100):
                downloader.enqueue_items(todo[i:i + 100], f"Playlist · {name or 'importada'}")
    finally:
        syncing.discard(source_id)


def due_sources():
    hours = app_settings.get_all()["pl_sync_hours"]
    return [r["id"] for r in rows("SELECT id FROM pl_sources WHERE enabled=1 AND (last_sync IS NULL OR last_sync < ?)",
                                 (int(time.time()) - hours * 3600,))]


def info(playlist_id):
    s = row("SELECT id, url, kind, name, enabled, last_sync, last_status, last_error, total, matched FROM pl_sources "
            "WHERE playlist_id=?", (playlist_id,))
    if s:
        s["syncing"] = s["id"] in syncing
        s["pending"] = row("SELECT COUNT(*) c FROM dl_items d JOIN pl_source_items i ON i.key=d.key "
                           "WHERE i.source_id=? AND d.status IN ('queued','running')", (s["id"],))["c"]
        s["enabled"] = bool(s["enabled"])
    return s
