"""Resumo da história do artista (Wikipédia, PT primeiro e depois EN) e foto do artista (Deezer).

Fica guardado na base de dados e é renovado de 30 em 30 dias. A Wikipédia só é aceite quando a página
é claramente sobre música (cantor, banda, DJ, rapper…), para não mostrar a história de outra pessoa.
"""
import json
import logging
import re
import time
import urllib.parse
import urllib.request

from . import dedupe, discover
from .db import get_conn

log = logging.getLogger("artistinfo")
SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_info (
    artist_id INTEGER PRIMARY KEY, bio TEXT, bio_url TEXT, bio_lang TEXT, photo TEXT, fans INTEGER, updated INTEGER
);
"""
MUSIC_WORDS = re.compile(r"cantor|cantora|band|banda|grupo|músic|music|singer|rapper|\bdj\b|produtor|producer|compositor|"
                         r"songwriter|duo|trio|orquestra|orchestra|vocalist|vocalista|guitarr|guitarist|artista|musician|boy band|girl group",
                         re.I)
UA = {"User-Agent": "nPlay/1.0 (servidor pessoal de música)"}
MAX_AGE = 30 * 86400


def init():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


def _json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _wiki(name, lang):
    q = urllib.parse.quote(f'"{name}" música OR banda OR cantor OR singer OR band' if lang == "pt" else f'"{name}" musician OR band OR singer OR rapper')
    found = _json(f"https://{lang}.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit=5&format=json")
    want = dedupe.norm(name)
    for hit in (found.get("query") or {}).get("search", []):
        title = hit["title"]
        base = dedupe.norm(re.sub(r"\s*\(.*?\)", "", title))
        if base != want:
            continue
        s = _json(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}")
        text = (s.get("extract") or "").strip()
        desc = (s.get("description") or "") + " " + text[:300]
        if s.get("type") == "disambiguation" or not text or not MUSIC_WORDS.search(desc):
            continue
        # resumo curto: as primeiras frases até ~420 caracteres
        sents = re.split(r"(?<=[.!?])\s+", text)
        short = ""
        for sn in sents:
            if len(short) + len(sn) > 420 and short:
                break
            short = (short + " " + sn).strip()
        return {"bio": short, "bio_url": (s.get("content_urls") or {}).get("desktop", {}).get("page"), "bio_lang": lang}
    return None


def _deezer_photo(name):
    found = discover._get("/search/artist", {"q": name, "limit": 5}, ttl=86400).get("data") or []
    want = dedupe.norm(name)
    a = next((x for x in found if dedupe.norm(x.get("name", "")) == want), None)
    if not a:
        return None, None
    return a.get("picture_xl") or a.get("picture_big"), a.get("nb_fan")


def get(artist_id, name):
    conn = get_conn()
    r = conn.execute("SELECT * FROM artist_info WHERE artist_id=?", (artist_id,)).fetchone()
    if r and time.time() - (r["updated"] or 0) < MAX_AGE:
        return dict(r)
    info = {"bio": None, "bio_url": None, "bio_lang": None, "photo": None, "fans": None}
    for lang in ("pt", "en"):
        try:
            w = _wiki(name, lang)
        except Exception as e:  # noqa
            log.info("Wikipédia %s indisponível: %s", lang, e)
            w = None
        if w:
            info.update(w)
            break
    try:
        info["photo"], info["fans"] = _deezer_photo(name)
    except Exception:  # noqa
        pass
    conn.execute("""INSERT INTO artist_info(artist_id, bio, bio_url, bio_lang, photo, fans, updated) VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(artist_id) DO UPDATE SET bio=excluded.bio, bio_url=excluded.bio_url, bio_lang=excluded.bio_lang,
                    photo=excluded.photo, fans=excluded.fans, updated=excluded.updated""",
                 (artist_id, info["bio"], info["bio_url"], info["bio_lang"], info["photo"], info["fans"], int(time.time())))
    conn.commit()
    return {"artist_id": artist_id, **info}
