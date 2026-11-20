import contextvars
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    image TEXT
);
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    year TEXT,
    cover TEXT,
    added_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    UNIQUE(title, artist_id)
);
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    artist_id INTEGER REFERENCES artists(id),
    album_id INTEGER REFERENCES albums(id),
    track_no INTEGER,
    disc_no INTEGER,
    duration REAL,
    size INTEGER,
    mtime REAL,
    added_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album_id);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist_id);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    added_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_pt_playlist ON playlist_tracks(playlist_id, position);
CREATE TABLE IF NOT EXISTS likes (
    user_id INTEGER NOT NULL DEFAULT 1,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    liked_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (user_id, track_id)
);
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    played_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_plays_time ON plays(played_at);
CREATE TABLE IF NOT EXISTS dl_items (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    artist TEXT,
    title TEXT,
    duration REAL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE INDEX IF NOT EXISTS idx_dli_key ON dl_items(key, status);
CREATE TABLE IF NOT EXISTS lyrics (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    synced TEXT,
    plain TEXT,
    source TEXT,
    fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pl_sources (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL UNIQUE REFERENCES playlists(id) ON DELETE CASCADE,
    url TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    name TEXT,
    cover TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    total INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    last_sync INTEGER,
    last_status TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);
CREATE TABLE IF NOT EXISTS pl_source_items (
    source_id INTEGER NOT NULL REFERENCES pl_sources(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    key TEXT NOT NULL,
    title TEXT,
    artist TEXT,
    duration REAL,
    query TEXT,
    PRIMARY KEY (source_id, position)
);
CREATE TABLE IF NOT EXISTS canvas (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    tried_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    total INTEGER DEFAULT 0,
    done INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    log TEXT DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    finished_at INTEGER
);
"""

_local = threading.local()
# utilizador do pedido atual (ver users.py); as consultas usam a função SQL uid()
CURRENT_UID = contextvars.ContextVar("uid", default=1)


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.create_function("uid", 0, lambda: CURRENT_UID.get())
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    # migração: coluna "label" nos downloads (bases de dados criadas antes desta versão)
    migrated = set()
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    if "genre" not in tcols:   # versões anteriores não guardavam o género: obriga a reler as etiquetas uma vez
        conn.execute("ALTER TABLE tracks ADD COLUMN genre TEXT")
        migrated.add("genre")
    for col, decl in (("genre_group", "TEXT"), ("release_date", "TEXT"), ("enriched", "INTEGER DEFAULT 0")):
        if col not in tcols:      # organização por género e data de lançamento
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {decl}")
            migrated.add(col)
    # vários utilizadores: gostos, histórico e playlists passam a ter dono (a conta 1 fica com o que já existia)
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(likes)")}:
        conn.executescript("""
            CREATE TABLE likes_mu (user_id INTEGER NOT NULL DEFAULT 1, track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
                                   liked_at INTEGER NOT NULL DEFAULT (strftime('%s','now')), PRIMARY KEY (user_id, track_id));
            INSERT INTO likes_mu(user_id, track_id, liked_at) SELECT 1, track_id, liked_at FROM likes;
            DROP TABLE likes;
            ALTER TABLE likes_mu RENAME TO likes;
        """)
    if "user_id" not in {r[1] for r in conn.execute("PRAGMA table_info(plays)")}:
        conn.execute("ALTER TABLE plays ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plays_user ON plays(user_id, played_at)")
    if "owner_id" not in {r[1] for r in conn.execute("PRAGMA table_info(playlists)")}:
        conn.execute("ALTER TABLE playlists ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 1")
    scols = {r[1] for r in conn.execute("PRAGMA table_info(pl_sources)")}
    for col, decl in (("interval_hours", "INTEGER"), ("auto_download", "INTEGER DEFAULT 1"),
                      ("spotify_mode", "TEXT DEFAULT 'auto'"), ("last_added", "INTEGER DEFAULT 0")):
        if col not in scols:      # parâmetros de atualização por playlist monitorizada
            conn.execute(f"ALTER TABLE pl_sources ADD COLUMN {col} {decl}")
    if "manual" not in {r[1] for r in conn.execute("PRAGMA table_info(playlist_tracks)")}:
        # músicas que tu adicionaste a uma playlist sincronizada (a sincronização não as apaga)
        conn.execute("ALTER TABLE playlist_tracks ADD COLUMN manual INTEGER DEFAULT 0")
    if "release_date" not in {r[1] for r in conn.execute("PRAGMA table_info(albums)")}:
        conn.execute("ALTER TABLE albums ADD COLUMN release_date TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_genre ON tracks(genre_group)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_release ON tracks(release_date)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "label" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN label TEXT")
    conn.execute("DELETE FROM dl_items WHERE status NOT IN ('queued','running') AND created_at < strftime('%s','now') - 2592000")
    conn.commit()
    return migrated


def rows(sql, params=()):
    return [dict(r) for r in get_conn().execute(sql, params).fetchall()]


def row(sql, params=()):
    r = get_conn().execute(sql, params).fetchone()
    return dict(r) if r else None
