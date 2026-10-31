"""Configurações guardadas na base de dados (partilhadas por todos os dispositivos)."""
import json
import re
from pathlib import Path

from .config import MUSIC_DIR, SPOTDL_BITRATE, SPOTDL_FORMAT, SPOTDL_OUTPUT, SPOTDL_THREADS
from .db import get_conn

STRUCTURES = {
    "artist_album": "{album-artist}/{album}/{track-number} - {title}.{output-ext}",
    "artist_flat": "{album-artist}/{artist} - {title}.{output-ext}",
    "album": "{album}/{track-number} - {title}.{output-ext}",
    "flat": "{artist} - {title}.{output-ext}",
}
FORMATS = ("mp3", "m4a", "opus", "flac", "ogg", "wav")
BITRATES = ("auto", "128k", "192k", "256k", "320k")
SCAN_OPTIONS = (0, 15, 60, 360, 1440)
LOSSLESS = {"flac", "wav"}


def _default_structure():
    for key, tpl in STRUCTURES.items():
        if tpl == SPOTDL_OUTPUT:
            return key
    return "custom"


def _clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


DEFAULTS = {
    "crossfade": 0,
    "auto_scan_min": 0,
    "dl_subfolder": "",
    "dl_structure": _default_structure(),
    "dl_template": SPOTDL_OUTPUT,
    "dl_format": SPOTDL_FORMAT if SPOTDL_FORMAT in FORMATS else "mp3",
    "dl_bitrate": SPOTDL_BITRATE if SPOTDL_BITRATE in BITRATES else "320k",
    "dl_threads": _clamp_int(SPOTDL_THREADS, 1, 4, 2),
}

class SettingError(ValueError):
    pass


BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\\]')


def _validate(key, v):
    if key == "crossfade":
        i = int(v)
        if not 0 <= i <= 12:
            raise SettingError("O crossfade tem de estar entre 0 e 12 segundos.")
        return i
    if key == "auto_scan_min":
        i = int(v)
        if i not in SCAN_OPTIONS:
            raise SettingError("Intervalo de verificação inválido.")
        return i
    if key == "dl_subfolder":
        s = str(v or "").strip().strip("/")
        if not s:
            return ""
        if BAD_CHARS.search(s) or len(s) > 120:
            raise SettingError("A subpasta tem caracteres inválidos (use apenas nomes de pastas separados por /).")
        parts = [p.strip() for p in s.split("/") if p.strip()]
        if any(p in (".", "..") for p in parts):
            raise SettingError("A subpasta não pode conter '.' nem '..'.")
        return "/".join(parts)
    if key == "dl_structure":
        if v != "custom" and v not in STRUCTURES:
            raise SettingError("Estrutura de pastas inválida.")
        return v
    if key == "dl_template":
        s = str(v or "").strip()
        if "{output-ext}" not in s:
            raise SettingError("O modelo tem de terminar com {output-ext} (a extensão do ficheiro).")
        if s.startswith("/") or ".." in s or len(s) > 200 or "\x00" in s:
            raise SettingError("Modelo inválido: não use caminhos absolutos nem '..'.")
        return s
    if key == "dl_format":
        if v not in FORMATS:
            raise SettingError("Formato inválido.")
        return v
    if key == "dl_bitrate":
        if v not in BITRATES:
            raise SettingError("Qualidade inválida.")
        return v
    if key == "dl_threads":
        i = int(v)
        if not 1 <= i <= 4:
            raise SettingError("Downloads simultâneos: entre 1 e 4.")
        return i
    raise SettingError("Definição desconhecida.")


def get_all():
    vals = dict(DEFAULTS)
    for r in get_conn().execute("SELECT key, value FROM settings"):
        if r["key"] in DEFAULTS:
            try:
                vals[r["key"]] = _validate(r["key"], json.loads(r["value"]))
            except Exception:
                pass
    return vals


def update(changes: dict):
    clean = {}
    for k, v in changes.items():
        if k in DEFAULTS:
            try:
                clean[k] = _validate(k, v)
            except SettingError:
                raise
            except (TypeError, ValueError):
                raise SettingError("Valor inválido.")
    conn = get_conn()
    for k, v in clean.items():
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (k, json.dumps(v)),
        )
    conn.commit()
    return get_all()


def template(values=None):
    values = values or get_all()
    return STRUCTURES.get(values["dl_structure"]) or values["dl_template"]


def download_path(values=None):
    """Pasta onde o spotDL corre (sem a criar)."""
    values = values or get_all()
    sub = values["dl_subfolder"]
    d = (MUSIC_DIR / sub).resolve() if sub else MUSIC_DIR
    if d != MUSIC_DIR and MUSIC_DIR not in d.parents:
        return MUSIC_DIR
    return d
