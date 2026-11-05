"""Rádio automática: escolhe as próximas músicas quando a fila acaba, para a música nunca parar.

Cada faixa da biblioteca recebe uma pontuação em relação às músicas "semente" (as últimas tocadas):
  + mesmo género (etiquetas do ficheiro; o spotDL grava os géneros do artista)
  + mesmo artista
  + aparece nas mesmas playlists que as sementes
  + quantas vezes já a ouviste (as mais tocadas pesam mais)
  + está nas "Músicas de que gostas"
  - tocou há pouco (para não repetir)
Depois sorteia-se entre as melhores com probabilidade proporcional à pontuação. Assim é sempre
parecido com o que estava a tocar, mas nunca igual, e com no máximo 2 músicas seguidas do mesmo artista.
"""
import math
import random
import re
import time

from . import dedupe
from .db import get_conn

SPLIT = re.compile(r"\s*[;,/|]\s*")


def _genres(raw):
    full = {g.strip().lower() for g in SPLIT.split(raw or "") if g.strip()}
    words = {w for g in full for w in re.findall(r"[a-zà-ú0-9]+", g) if len(w) > 2}
    return full, words


def suggest(seeds, exclude=(), n=20):
    conn = get_conn()
    exclude = set(int(x) for x in exclude) | set(int(x) for x in seeds)
    now = int(time.time())
    tracks = conn.execute("SELECT id, artist, genre, album_id FROM tracks").fetchall()
    if not tracks:
        return []
    by_id = {t["id"]: t for t in tracks}
    seeds = [s for s in seeds if s in by_id]
    if not seeds:  # sem sementes: usa o que ouviste por último
        seeds = [r[0] for r in conn.execute("SELECT track_id FROM plays ORDER BY played_at DESC LIMIT 3") if r[0] in by_id]

    s_full, s_words, s_artists = set(), set(), set()
    for sid in seeds:
        f, w = _genres(by_id[sid]["genre"])
        s_full |= f
        s_words |= w
        s_artists.add(dedupe.primary_artist(by_id[sid]["artist"]))
    co = set()
    if seeds:
        q = ",".join("?" * len(seeds))
        co = {r[0] for r in conn.execute(
            f"SELECT DISTINCT track_id FROM playlist_tracks WHERE playlist_id IN "
            f"(SELECT playlist_id FROM playlist_tracks WHERE track_id IN ({q}))", seeds)}
    plays = dict(conn.execute("SELECT track_id, COUNT(*) FROM plays WHERE played_at > ? GROUP BY track_id",
                              (now - 180 * 86400,)).fetchall())
    recent = {r[0] for r in conn.execute("SELECT track_id FROM plays WHERE played_at > ?", (now - 3 * 3600,))}
    liked = {r[0] for r in conn.execute("SELECT track_id FROM likes")}

    scored = []
    for t in tracks:
        tid = t["id"]
        if tid in exclude:
            continue
        f, w = _genres(t["genre"])
        rel = 0.0
        if s_full and f:
            rel += 3.0 * min(2, len(f & s_full)) + 0.8 * min(3, len(w & s_words))
        if dedupe.primary_artist(t["artist"]) in s_artists:
            rel += 2.5
        if tid in co:
            rel += 2.0
        score = 0.3 + rel + 1.2 * math.log1p(plays.get(tid, 0)) + (1.5 if tid in liked else 0)
        if tid in recent:
            score *= 0.15
        scored.append((score, rel, tid, dedupe.primary_artist(t["artist"])))

    # o que tocou nas últimas horas fica de fora, se houver alternativas suficientes
    fresh = [x for x in scored if x[2] not in recent]
    if len(fresh) >= n:
        scored = fresh
    # parecidas primeiro; se forem poucas, junta as melhores das restantes (para haver variedade)
    related = sorted((x for x in scored if x[1] > 0), reverse=True)
    others = sorted((x for x in scored if x[1] <= 0), reverse=True)
    pool = related[: n * 4] if len(related) >= n * 2 else related + others[: n * 2 - len(related)]

    out, per_artist = [], {}
    rnd = random.Random()
    while pool and len(out) < n:
        weights = [max(0.05, p[0]) ** 1.5 for p in pool]
        i = rnd.choices(range(len(pool)), weights=weights, k=1)[0]
        score, rel, tid, art = pool.pop(i)
        if per_artist.get(art, 0) >= 2 and len(pool) > n - len(out):
            continue  # variedade: no máximo 2 do mesmo artista por lote
        per_artist[art] = per_artist.get(art, 0) + 1
        out.append(tid)
    return out
