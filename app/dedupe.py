"""Evita downloads repetidos.

Uma música é identificada por (título normalizado + artista principal) e, quando os dois lados
têm duração, por uma duração parecida (para não confundir a versão de estúdio com a ao vivo).
Verifica-se contra:
  1. a biblioteca (o que já está nos ficheiros),
  2. a fila (o que já foi pedido e ainda não terminou),
  3. o próprio pedido (a mesma faixa duas vezes).
O spotDL faz uma 4.ª verificação (--archive e --overwrite skip) para links do Spotify.
"""
import re
import unicodedata

from .db import get_conn


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s)          # (feat. X), [Remastered]
    s = re.split(r"\s-\s", s)[0]                    # "Título - Remastered 2011"
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def primary_artist(artist):
    first = re.split(r"\s*(?:,|;|&|/|\bfeat\.?|\bft\.?|\bfeaturing)\s*", artist or "", maxsplit=1, flags=re.I)[0]
    return norm(first)


def key_for(artist, title):
    return f"{norm(title)}|{primary_artist(artist)}"


def _close(a, b):
    if not a or not b:
        return True
    return abs(a - b) <= max(8, 0.05 * max(a, b))


def library_index():
    idx = {}
    for r in get_conn().execute("SELECT title, artist, duration FROM tracks"):
        k = key_for(r["artist"], r["title"])
        if not k.startswith("|"):
            idx.setdefault(k, []).append(r["duration"])
    return idx


def pending_keys():
    return {r[0] for r in get_conn().execute("SELECT DISTINCT key FROM dl_items WHERE status IN ('queued','running')")}


def status(title, artist, duration=None, lib=None, pend=None):
    """'library' | 'pending' | None"""
    lib = library_index() if lib is None else lib
    pend = pending_keys() if pend is None else pend
    k = key_for(artist, title)
    if k.startswith("|") or k.endswith("|"):
        return None
    if any(_close(duration, d) for d in lib.get(k, [])):
        return "library"
    if k in pend:
        return "pending"
    return None
