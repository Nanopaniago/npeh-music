#!/bin/sh
set -e
# O YouTube muda com frequência; atualizar o yt-dlp no arranque evita a maioria das falhas de download.
if [ "${UPDATE_YTDLP:-1}" = "1" ]; then
  echo "[pobrefy] a atualizar yt-dlp…"
  /opt/spotdl/bin/pip install -q --no-cache-dir -U yt-dlp >/dev/null 2>&1 || echo "[pobrefy] não foi possível atualizar o yt-dlp (sem rede?)"
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8800 --proxy-headers --forwarded-allow-ips="*" --log-level warning
