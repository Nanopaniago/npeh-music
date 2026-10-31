FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUSIC_DIR=/music \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# spotDL num ambiente isolado (evita conflitos de versões com a app)
RUN python -m venv /opt/spotdl \
 && /opt/spotdl/bin/pip install --no-cache-dir "spotdl>=4.2" \
 && ln -s /opt/spotdl/bin/spotdl /usr/local/bin/spotdl

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8800
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8800/api/me',timeout=4)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
