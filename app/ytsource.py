"""Fonte alternativa de áudio: yt-dlp direto (YouTube Music → YouTube → SoundCloud), sem passar pelo Spotify.

Usada quando o spotDL não consegue uma música (vídeo com restrição de idade, sem resultados, limite do Spotify…).
Procura vários candidatos, escolhe pela duração (Deezer) e pelo título, salta os bloqueados e tenta o seguinte.
A capa, o álbum, o número da faixa e a data vêm do Deezer e ficam embutidos no ficheiro.
"""
import base64
import json
import logging
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

from . import catalog
from . import settings as app_settings
from .config import DATA_DIR

log = logging.getLogger("ytsource")
COOKIES = DATA_DIR / "youtube-cookies.txt"
BAD_WORDS = ("live", "ao vivo", "en vivo", "remix", "cover", "karaoke", "instrumental", "8d", "sped up", "slowed",
             "nightcore", "reverb", "acapella", "a cappella", "tutorial", "reaction", "lyrics video", "1 hour", "loop")


def _ytdlp():
    p = shutil.which("spotdl")
    cand = Path(p).resolve().parent / "yt-dlp" if p else None
    return str(cand) if cand and cand.exists() else (shutil.which("yt-dlp") or "yt-dlp")


def _base():
    cmd = [_ytdlp(), "--no-warnings", "--no-playlist", "--socket-timeout", "20", "--retries", "10", "--fragment-retries", "10"]
    if COOKIES.exists() and COOKIES.stat().st_size > 0:
        cmd += ["--cookies", str(COOKIES)]
    return cmd


def _words(s):
    return {w for w in re.sub(r"[^\w]+", " ", (s or "").lower()).split() if len(w) > 1}


def _search(query, source, n=6):
    url = {"ytmusic": "https://music.youtube.com/search?q=" + urllib.parse.quote(query) + "#songs",
           "youtube": f"ytsearch{n}:{query}", "soundcloud": f"scsearch{n}:{query}"}[source]
    try:
        r = subprocess.run([*_base(), "--flat-playlist", "--playlist-end", str(n), "-J", url],
                           capture_output=True, text=True, timeout=90)
        data = json.loads(r.stdout or "{}")
    except Exception as e:  # noqa
        log.info("pesquisa %s falhou: %s", source, e)
        return []
    out = []
    for e in (data.get("entries") or [])[:n]:
        link = e.get("url") or e.get("webpage_url")
        if not link:
            continue
        if source != "soundcloud" and not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={e.get('id')}"
        out.append({"url": link, "title": e.get("title") or "", "duration": e.get("duration"),
                    "channel": e.get("channel") or e.get("uploader") or "", "source": source})
    return out


def _score(c, artist, title, duration):
    want_t, want_a = _words(title), _words(artist.split(",")[0])
    got = _words(c["title"] + " " + c["channel"])
    s = 0.0
    s += 4 * len(want_t & got) / max(1, len(want_t))
    s += 2 * len(want_a & got) / max(1, len(want_a))
    if " - topic" in c["channel"].lower() or c["source"] == "ytmusic":
        s += 3                                     # faixa oficial ("Artist - Topic" / YouTube Music): só o áudio
    low = c["title"].lower()
    if any(w in low for w in ("official video", "music video", "videoclipe", "clipe oficial", "video oficial")):
        s -= 1.5                                   # o videoclipe pode ter introduções e falas
    for w in BAD_WORDS:
        if w in low and w not in title.lower():
            s -= 3
    if duration and c.get("duration"):
        diff = abs(c["duration"] - duration)
        s += 3 if diff <= 3 else 1.5 if diff <= 8 else -1 if diff <= 20 else -6
    if c["source"] == "soundcloud" and c.get("duration") and c["duration"] <= 31:
        s -= 10                                    # pré-visualização de 30 s
    return s


def _fmt(s):
    fmt = s["dl_format"]
    if fmt == "original":
        return "opus", None
    return fmt, (None if fmt in app_settings.LOSSLESS else s["dl_bitrate"])


def _safe(x):
    return re.sub(r'[\\/:*?"<>|]+', "-", str(x or "")).strip(" .") or "Desconhecido"


def _out_template(s, meta):
    tpl = app_settings.template(s)
    vals = {"{artist}": meta["artist"], "{album-artist}": meta["album_artist"], "{album}": meta["album"],
            "{title}": meta["title"], "{track-number}": f"{meta['track']:02d}" if meta.get("track") else "01",
            "{year}": (meta.get("date") or "")[:4], "{genre}": ""}
    for k, v in vals.items():
        tpl = tpl.replace(k, _safe(v).replace("%", "%%"))
    return re.sub(r"\{[a-z-]+\}", "", tpl.replace("{output-ext}", "%(ext)s"))


def _meta(artist, title, duration):
    meta = {"artist": artist, "album_artist": artist.split(",")[0].strip(), "title": title, "album": title,
            "track": 1, "date": None, "cover": None, "duration": duration}
    try:
        t = catalog._best_match(artist, title, duration)
    except Exception:  # noqa
        t = None
    if t:
        alb = t.get("album") or {}
        meta.update(album=alb.get("title") or title, cover=alb.get("cover_xl") or alb.get("cover_big"),
                    duration=duration or t.get("duration"))
        try:
            full = catalog.discover._get(f"/track/{int(t['id'])}", ttl=86400)
            meta["track"] = full.get("track_position") or 1
            meta["date"] = full.get("release_date") or (full.get("album") or {}).get("release_date")
        except Exception:  # noqa
            pass
    return meta


def _cover_bytes(url):
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read()
    except Exception:  # noqa
        return None


def tag(path: Path, meta: dict):
    """Etiquetas + capa embutida (mp3, m4a, opus/ogg, flac)."""
    cover = _cover_bytes(meta.get("cover"))
    ext = path.suffix.lower()
    f = mutagen.File(path, easy=True)
    if f is None:
        return
    if f.tags is None:
        f.add_tags()
    f["artist"], f["title"], f["album"] = meta["artist"], meta["title"], meta["album"]
    try:
        f["albumartist"] = meta["album_artist"]
    except Exception:  # noqa
        pass
    f["tracknumber"] = str(meta.get("track") or 1)
    if meta.get("date"):
        f["date"] = meta["date"]
    f.save()
    if not cover:
        return
    if ext == ".mp3":
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        id3.save(path)
    elif ext in (".m4a", ".mp4", ".aac"):
        m = MP4(path)
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
        m.save()
    elif ext == ".flac":
        fl = FLAC(path)
        pic = Picture(); pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
        fl.clear_pictures(); fl.add_picture(pic); fl.save()
    elif ext in (".opus", ".ogg"):
        o = mutagen.File(path)
        pic = Picture(); pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
        o["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
        o.save()


def fetch(artist, title, duration, stage: Path, s: dict, logf=lambda _l: None):
    """Descarrega uma música para `stage`. -> caminho do ficheiro ou None."""
    meta = _meta(artist, title, duration)
    duration = meta["duration"]
    q = f"{artist.split(',')[0].strip()} {title}"
    cands, seen = [], set()
    for src in ("ytmusic", "youtube", "soundcloud"):
        for c in _search(q if src != "youtube" else q + " audio", src):
            if c["url"] not in seen:
                seen.add(c["url"]); cands.append(c)
        good = [c for c in cands if _score(c, artist, title, duration) >= 5]
        if len(good) >= 2:
            break
    cands.sort(key=lambda c: -_score(c, artist, title, duration))
    cands = [c for c in cands if _score(c, artist, title, duration) >= 3][:5]
    if not cands:
        logf(f"  · fonte alternativa: sem resultados para «{artist} - {title}»")
        return None
    fmt, bitrate = _fmt(s)
    out = _out_template(s, meta)
    for c in cands:
        before = {p for p in stage.rglob("*") if p.is_file()}
        cmd = [*_base(), "-f", "bestaudio/best", "-x", "--audio-format", fmt, "-o", out, c["url"]]
        if bitrate and bitrate not in ("disable", "auto"):
            cmd[-1:-1] = ["--audio-quality", bitrate.upper().rstrip("K") + "K"]
        try:
            r = subprocess.run(cmd, cwd=str(stage), capture_output=True, text=True, timeout=420)
        except subprocess.TimeoutExpired:
            logf(f"  · {c['source']}: demorou demasiado, a tentar outro")
            continue
        new = [p for p in stage.rglob("*") if p.is_file() and p not in before and not p.name.endswith((".part", ".ytdl", ".webp", ".jpg"))]
        if r.returncode != 0 or not new:
            why = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["erro"]
            reason = "restrição de idade" if "age" in why[0].lower() or "sign in" in why[0].lower() else why[0][:120]
            logf(f"  · {c['source']} «{c['title'][:60]}»: {reason} — a tentar o seguinte")
            for p in new:
                p.unlink(missing_ok=True)
            continue
        path = new[0]
        try:
            tag(path, meta)
        except Exception as e:  # noqa
            log.info("etiquetas falharam em %s: %s", path, e)
        logf(f"  ✓ fonte alternativa ({c['source']}): «{c['title'][:70]}»")
        return path
    return None
