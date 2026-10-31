import os
import secrets
from pathlib import Path

MUSIC_DIR = Path(os.getenv("MUSIC_DIR", "/music")).resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", "/data")).resolve()
COVERS_DIR = DATA_DIR / "covers"
DB_PATH = DATA_DIR / "library.db"

# Login (deixa vazio para desativar — só recomendado se não estiver exposto à internet)
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")

# spotDL
SPOTDL_FORMAT = os.getenv("SPOTDL_FORMAT", "mp3")
SPOTDL_BITRATE = os.getenv("SPOTDL_BITRATE", "320k")
SPOTDL_THREADS = os.getenv("SPOTDL_THREADS", "2")
SPOTDL_OUTPUT = os.getenv(
    "SPOTDL_OUTPUT", "{album-artist}/{album}/{track-number} - {title}.{output-ext}"
)
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTDL_EXTRA_ARGS = os.getenv("SPOTDL_EXTRA_ARGS", "")

SCAN_ON_START = os.getenv("SCAN_ON_START", "1") == "1"
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".mp4", ".aac", ".ogg", ".opus", ".wav", ".wma"}

for d in (MUSIC_DIR, DATA_DIR, COVERS_DIR):
    d.mkdir(parents=True, exist_ok=True)

if not SECRET_KEY:
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        SECRET_KEY = key_file.read_text().strip()
    else:
        SECRET_KEY = secrets.token_hex(32)
        key_file.write_text(SECRET_KEY)
