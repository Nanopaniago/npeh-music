"""Músicas repetidas na biblioteca: encontrar, sugerir qual manter e remover as cópias com segurança.

Duas faixas são a mesma música quando têm o mesmo título e artista principal (normalizados) e
duração parecida. Ao remover:
  - as playlists, os gostos, o histórico e as letras passam para a cópia que fica;
  - os ficheiros não são apagados: vão para a pasta ".pobrefy-lixo" dentro da pasta da música
    (no mesmo disco, por isso é instantâneo), de onde podem ser recuperados à mão.
"""
import shutil
import time
from pathlib import Path

import mutagen

from . import dedupe
from .config import COVERS_DIR, DATA_DIR, MUSIC_DIR
from .db import get_conn

TRASH = MUSIC_DIR / ".pobrefy-lixo"
LOSSLESS = {".flac", ".wav", ".aiff", ".alac"}


def _file_info(rel):
    p = MUSIC_DIR / rel
    info = {"ext": p.suffix.lower().lstrip("."), "bitrate": None, "exists": p.exists()}
    try:
        f = mutagen.File(p)
        if f is not None and getattr(f.info, "bitrate", None):
            info["bitrate"] = round(f.info.bitrate / 1000)
    except Exception:  # noqa
        pass
    info["lossless"] = p.suffix.lower() in LOSSLESS
    return info


def find_groups():
    conn = get_conn()
    tracks = [dict(r) for r in conn.execute(
        "SELECT t.id, t.path, t.title, t.artist, t.duration, t.size, t.added_at, t.album_id, "
        "al.title AS album, al.cover IS NOT NULL AS has_cover FROM tracks t LEFT JOIN albums al ON al.id=t.album_id")]
    by_key = {}
    for t in tracks:
        k = dedupe.key_for(t["artist"], t["title"])
        if k.startswith("|") or k.endswith("|"):
            continue
        by_key.setdefault(k, []).append(t)

    plays = dict(conn.execute("SELECT track_id, COUNT(*) FROM plays GROUP BY track_id").fetchall())
    in_pl = dict(conn.execute("SELECT track_id, COUNT(DISTINCT playlist_id) FROM playlist_tracks GROUP BY track_id").fetchall())
    liked = {r[0] for r in conn.execute("SELECT track_id FROM likes")}

    groups = []
    for key, items in by_key.items():
        if len(items) < 2:
            continue
        # junta por duração parecida (versão de estúdio ≠ ao vivo)
        items.sort(key=lambda t: t["duration"] or 0)
        clusters = []
        for t in items:
            if clusters and dedupe._close(clusters[-1][0]["duration"], t["duration"]):
                clusters[-1].append(t)
            else:
                clusters.append([t])
        for c in clusters:
            if len(c) < 2:
                continue
            for t in c:
                t.update(_file_info(t["path"]))
                t["plays"] = plays.get(t["id"], 0)
                t["playlists"] = in_pl.get(t["id"], 0)
                t["liked"] = t["id"] in liked
                t["has_cover"] = bool(t["has_cover"])
            # sugestão: melhor qualidade, depois a mais usada, depois a mais antiga
            best = max(c, key=lambda t: (t["exists"], t["lossless"], t["bitrate"] or 0, t["plays"] + t["playlists"] * 3 + t["liked"] * 3, -t["added_at"]))
            groups.append({"key": key, "title": best["title"], "artist": best["artist"], "keep": best["id"],
                           "tracks": sorted(c, key=lambda t: t["id"] != best["id"])})
    groups.sort(key=lambda g: (g["artist"].lower(), g["title"].lower()))
    extra = [t for g in groups for t in g["tracks"] if t["id"] != g["keep"]]
    return {"groups": groups, "copies": len(extra), "bytes": sum(t["size"] or 0 for t in extra)}


def _move_to_trash(rel):
    src = MUSIC_DIR / rel
    moved = []
    stem = src.with_suffix("")
    sidecars = [src, src.with_suffix(".lrc"), Path(str(src) + ".lrc")] + [Path(str(stem) + ".canvas" + e) for e in (".mp4", ".webm")]
    for p in sidecars:
        if p.exists():
            dst = TRASH / p.relative_to(MUSIC_DIR)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = dst.with_name(f"{dst.stem}.{int(time.time())}{dst.suffix}")
            shutil.move(str(p), str(dst))
            moved.append(str(dst.relative_to(MUSIC_DIR)))
    # remove pastas que ficaram vazias (álbuns, artistas)
    d = src.parent
    while d != MUSIC_DIR and d.exists() and MUSIC_DIR in d.parents:
        try:
            leftovers = [x for x in d.iterdir() if x.name.lower() not in ("cover.jpg", "cover.png", "folder.jpg", ".ds_store", "thumbs.db")]
            if leftovers:
                break
            for x in d.iterdir():
                x.unlink()
            d.rmdir()
        except OSError:
            break
        d = d.parent
    return moved


def remove(plan):
    """plan = [{"keep": id, "remove": [ids]}]. Só remove faixas que são mesmo cópias da que fica."""
    conn = get_conn()
    done, skipped, moved = 0, 0, 0
    for g in plan:
        keep = conn.execute("SELECT id, title, artist, duration FROM tracks WHERE id=?", (int(g["keep"]),)).fetchone()
        if not keep:
            skipped += len(g.get("remove", []))   # a faixa a manter já não existe
            continue
        k = dedupe.key_for(keep["artist"], keep["title"])
        for rid in {int(x) for x in g.get("remove", [])} - {keep["id"]}:
            r = conn.execute("SELECT id, path, title, artist, duration FROM tracks WHERE id=?", (rid,)).fetchone()
            if not r or dedupe.key_for(r["artist"], r["title"]) != k or not dedupe._close(r["duration"], keep["duration"]):
                skipped += 1      # proteção: não é uma cópia desta música
                continue
            kid = keep["id"]
            # playlists: a cópia que fica entra no lugar da removida (sem ficar repetida)
            for pl, pos in conn.execute("SELECT playlist_id, position FROM playlist_tracks WHERE track_id=?", (rid,)).fetchall():
                has = conn.execute("SELECT 1 FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pl, kid)).fetchone()
                if has:
                    conn.execute("DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?", (pl, rid))
                else:
                    conn.execute("UPDATE playlist_tracks SET track_id=? WHERE playlist_id=? AND track_id=?", (kid, pl, rid))
            if conn.execute("SELECT 1 FROM likes WHERE track_id=?", (rid,)).fetchone():
                conn.execute("INSERT OR IGNORE INTO likes(track_id) VALUES (?)", (kid,))
            conn.execute("UPDATE plays SET track_id=? WHERE track_id=?", (kid, rid))
            if not conn.execute("SELECT 1 FROM lyrics WHERE track_id=? AND (synced IS NOT NULL OR plain IS NOT NULL)", (kid,)).fetchone():
                conn.execute("DELETE FROM lyrics WHERE track_id=?", (kid,))
                conn.execute("UPDATE OR IGNORE lyrics SET track_id=? WHERE track_id=?", (kid, rid))
            moved += len(_move_to_trash(r["path"]))
            conn.execute("DELETE FROM tracks WHERE id=?", (rid,))
            (DATA_DIR / "canvas" / f"{rid}.mp4").unlink(missing_ok=True)
            done += 1
        conn.commit()
    # álbuns e artistas que ficaram sem faixas
    for (aid,) in conn.execute("SELECT id FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)").fetchall():
        (COVERS_DIR / f"album_{aid}.jpg").unlink(missing_ok=True)
    conn.execute("DELETE FROM albums WHERE id NOT IN (SELECT DISTINCT album_id FROM tracks WHERE album_id IS NOT NULL)")
    conn.execute("DELETE FROM artists WHERE id NOT IN (SELECT DISTINCT artist_id FROM tracks WHERE artist_id IS NOT NULL) "
                 "AND id NOT IN (SELECT artist_id FROM albums)")
    conn.commit()
    return {"removed": done, "skipped": skipped, "files": moved}


def trash_info():
    if not TRASH.exists():
        return {"files": 0, "bytes": 0, "path": str(TRASH)}
    files = [p for p in TRASH.rglob("*") if p.is_file()]
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files), "path": str(TRASH)}


def empty_trash():
    info = trash_info()
    if TRASH.exists():
        shutil.rmtree(TRASH, ignore_errors=True)
    return info
