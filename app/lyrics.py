"""Letras: ficheiro .lrc ao lado da música → letra embutida nas tags → LRCLIB (grátis, sem chave).

O resultado fica em cache na base de dados. Quando a letra sincronizada vem do LRCLIB, grava-se
também um .lrc ao lado do ficheiro (outros players também o usam).
"""
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags

from .config import MUSIC_DIR
from .db import get_conn, row

log = logging.getLogger("lyrics")
LRCLIB = "https://lrclib.net/api"
RETRY_AFTER = 7 * 86400
RE_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
state = {"running": False, "done": 0, "total": 0}


def parse_lrc(text):
    out = []
    for line in (text or "").splitlines():
        stamps = RE_TS.findall(line)
        if not stamps:
            continue
        body = RE_TS.sub("", line).strip()
        for m, s in stamps:
            out.append({"t": round(int(m) * 60 + float(s.replace(":", ".")), 2), "text": body})
    out.sort(key=lambda x: x["t"])
    return out


def _looks_synced(text):
    return len(RE_TS.findall(text or "")) >= 3


def _embedded(path: Path):
    try:
        f = mutagen.File(path)
        if f is None or f.tags is None:
            return None
        tags = f.tags
        if isinstance(tags, ID3):                         # mp3
            for fr in tags.getall("USLT"):
                if fr.text:
                    return str(fr.text)
        elif isinstance(tags, MP4Tags):                   # m4a
            v = tags.get("\xa9lyr")
            return str(v[0]) if v else None
        else:                                             # vorbis (opus, ogg, flac)
            for k in ("lyrics", "unsyncedlyrics", "syncedlyrics"):
                v = tags.get(k)
                if v:
                    return str(v[0] if isinstance(v, list) else v)
    except Exception:
        pass
    return None


def _lrclib(artist, title, album, duration):
    ua = {"User-Agent": "Pobrefy/1.0 (https://github.com/Nanopaniago/npeh-music)"}

    def get(url):
        req = urllib.request.Request(url, headers=ua)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)

    q = {"artist_name": artist, "track_name": title}
    if album:
        q["album_name"] = album
    if duration:
        q["duration"] = int(round(duration))
    try:
        d = get(f"{LRCLIB}/get?{urllib.parse.urlencode(q)}")
        if d.get("syncedLyrics") or d.get("plainLyrics"):
            return d
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    res = get(f"{LRCLIB}/search?{urllib.parse.urlencode({'track_name': title, 'artist_name': artist})}")
    best = None
    for d in res if isinstance(res, list) else []:
        if duration and d.get("duration") and abs(d["duration"] - duration) > 8:
            continue
        if d.get("syncedLyrics"):
            return d
        best = best or (d if d.get("plainLyrics") else None)
    return best


def _save(track_id, synced, plain, source):
    conn = get_conn()
    conn.execute(
        "INSERT INTO lyrics(track_id, synced, plain, source, fetched_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(track_id) DO UPDATE SET synced=excluded.synced, plain=excluded.plain, "
        "source=excluded.source, fetched_at=excluded.fetched_at",
        (track_id, synced, plain, source, int(time.time())),
    )
    conn.commit()


def _result(r):
    synced = parse_lrc(r["synced"]) if r.get("synced") else None
    return {"synced": synced or None, "plain": r.get("plain") or None, "source": r.get("source")}


def get(track_id, online=True):
    cached = row("SELECT * FROM lyrics WHERE track_id=?", (track_id,))
    if cached and (cached["synced"] or cached["plain"] or time.time() - cached["fetched_at"] < RETRY_AFTER):
        return _result(cached)
    t = row("SELECT t.path, t.title, t.artist, t.duration, al.title AS album FROM tracks t "
            "LEFT JOIN albums al ON al.id=t.album_id WHERE t.id=?", (track_id,))
    if not t:
        return {"synced": None, "plain": None, "source": None}
    path = (MUSIC_DIR / t["path"])
    # 1) .lrc ao lado do ficheiro
    for lrc in (path.with_suffix(".lrc"), Path(str(path) + ".lrc")):
        if lrc.exists():
            try:
                text = lrc.read_text(encoding="utf-8", errors="replace")
                if _looks_synced(text):
                    _save(track_id, text, None, "ficheiro .lrc")
                    return _result({"synced": text, "plain": None, "source": "ficheiro .lrc"})
            except OSError:
                pass
    # 2) letra embutida (o spotDL grava-a nas tags)
    emb = _embedded(path)
    if emb:
        if _looks_synced(emb):
            _save(track_id, emb, None, "etiquetas do ficheiro")
            return _result({"synced": emb, "plain": None, "source": "etiquetas do ficheiro"})
        plain_emb = emb
    else:
        plain_emb = None
    # 3) LRCLIB
    if online:
        try:
            d = _lrclib(t["artist"].split(",")[0].strip(), re.sub(r"\s*\((feat|ft)\.?[^)]*\)", "", t["title"], flags=re.I), t["album"], t["duration"])
        except Exception as e:  # noqa
            log.info("LRCLIB falhou para %s: %s", t["title"], e)
            return {"synced": None, "plain": plain_emb, "source": "etiquetas do ficheiro" if plain_emb else None}
        if d and d.get("syncedLyrics"):
            _save(track_id, d["syncedLyrics"], d.get("plainLyrics"), "LRCLIB")
            try:
                lrc = path.with_suffix(".lrc")
                if not lrc.exists():
                    lrc.write_text(d["syncedLyrics"], encoding="utf-8")
            except OSError:
                pass
            return _result({"synced": d["syncedLyrics"], "plain": d.get("plainLyrics"), "source": "LRCLIB"})
        if d and d.get("plainLyrics"):
            _save(track_id, None, d["plainLyrics"], "LRCLIB")
            return _result({"synced": None, "plain": d["plainLyrics"], "source": "LRCLIB"})
    if plain_emb:
        _save(track_id, None, plain_emb, "etiquetas do ficheiro")
        return _result({"synced": None, "plain": plain_emb, "source": "etiquetas do ficheiro"})
    _save(track_id, None, None, None)
    return {"synced": None, "plain": None, "source": None}


def stats():
    r = row("SELECT (SELECT COUNT(*) FROM tracks) AS tracks, "
            "(SELECT COUNT(*) FROM lyrics WHERE synced IS NOT NULL OR plain IS NOT NULL) AS with_lyrics, "
            "(SELECT COUNT(*) FROM lyrics WHERE synced IS NOT NULL) AS synced")
    return {**r, **state}


_lock = threading.Lock()


def prefetch(limit=None):
    """Procura letras das músicas que ainda não foram verificadas (mais recentes primeiro)."""
    if not _lock.acquire(blocking=False):
        return
    try:
        conn = get_conn()
        sql = ("SELECT t.id FROM tracks t LEFT JOIN lyrics l ON l.track_id=t.id "
               "WHERE l.track_id IS NULL OR (l.synced IS NULL AND l.plain IS NULL AND l.fetched_at < strftime('%s','now') - ?) "
               "ORDER BY t.added_at DESC, t.id DESC")
        ids = [r[0] for r in conn.execute(sql + (f" LIMIT {int(limit)}" if limit else ""), (RETRY_AFTER,))]
        state.update(running=True, done=0, total=len(ids))
        for tid in ids:
            try:
                get(tid)
            except Exception:  # noqa
                log.exception("letras: falhou %s", tid)
            state["done"] += 1
            time.sleep(0.4)  # simpático com o LRCLIB
    finally:
        state["running"] = False
        _lock.release()
