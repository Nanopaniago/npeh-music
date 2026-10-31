# Pobrefy

O Downtify e o Swing Music numa só app, ao estilo Spotify, alojada no teu DietPi.

- **Downloads**: cola links do Spotify (faixa, álbum, playlist ou artista) ou escreve "artista - música". O spotDL descarrega e a biblioteca atualiza-se sozinha.
- **Streaming**: início, pesquisa, álbuns, artistas, playlists, músicas de que gostas, fila, aleatório e repetir. Também tem controlos no ecrã de bloqueio do telemóvel.
- **PWA**: instala-se no telemóvel e funciona através do Cloudflare Tunnel, protegida por login.
- **Leve**: um único container com FastAPI e SQLite. Fica com cerca de 80–150 MB de RAM em repouso, limitado a 512 MB no compose.

---

## 1. Antes de começar: descobrir a pasta de música atual

O Pobrefy reutiliza os ficheiros que já tens. Para descobrires onde estão:

```bash
docker inspect downtify --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
docker inspect swingmusic --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

(Se os nomes dos containers forem outros, vê-os com `docker ps`.)

Usa a pasta de origem (`Source`) dos downloads do Downtify. Em princípio é a mesma que o Swing lê.

## 2. Instalar

```bash
# copia a pasta npeh-music para o servidor, por exemplo:
scp -r npeh-music dietpi@192.168.1.81:/opt/

ssh dietpi@192.168.1.81
cd /opt/npeh-music
cp .env.example .env
nano .env                       # define APP_PASSWORD (e, de preferência, as credenciais Spotify)
nano docker-compose.yml         # troca /mnt/dietpi_userdata/music pela pasta do passo 1
docker compose up -d --build
```

Abre `http://192.168.1.81:8800`. A primeira leitura da biblioteca é feita ao arrancar; com alguns milhares de faixas demora um ou dois minutos.

**CasaOS:** também podes usar *App Store → Custom Install → Import* e colar o `docker-compose.yml`. Primeiro tens de construir a imagem com `docker compose build` no terminal, porque a imagem é local.

## 3. Desligar o Downtify e o Swing Music

Quando confirmares que está tudo a funcionar:

```bash
docker stop downtify swingmusic
# depois de uns dias sem problemas:
docker rm downtify swingmusic
```

As playlists do Swing não são importadas. Os ficheiros de música ficam exatamente onde estavam.

## 4. Acesso fora de casa (Cloudflare Tunnel)

No painel Zero Trust → Networks → Tunnels → o teu túnel → *Public Hostname*:

| Subdomínio | Domínio | Serviço |
|---|---|---|
| `music` | `digitalnpeh.xyz` | `http://localhost:8800` |

Depois, no telemóvel, abre `https://music.digitalnpeh.xyz`:

- **Android (Chrome):** menu ⋮ → *Instalar app*
- **iPhone (Safari):** Partilhar → *Adicionar ao ecrã principal*

> Define sempre `APP_PASSWORD` antes de expor a app. Para uma camada extra, podes pôr o subdomínio atrás do Cloudflare Access (login por e-mail).

## 5. Configuração (`.env`)

| Variável | Para que serve |
|---|---|
| `APP_PASSWORD` | Palavra-passe de acesso. Se ficar vazia, o login fica desligado (usa só em rede local). |
| `SPOTIFY_CLIENT_ID` / `SECRET` | Credenciais próprias da Spotify API, que evitam erros de *rate limit*. Cria-as em developer.spotify.com. |
| `SPOTDL_FORMAT` / `BITRATE` | `mp3`/`320k` por omissão. Também aceita `m4a`, `opus`, `flac`… |
| `SPOTDL_OUTPUT` | Modelo das pastas. Por omissão: `Artista/Álbum/01 - Título.mp3`. |
| `SPOTDL_EXTRA_ARGS` | Argumentos extra, por exemplo `--cookie-file /data/cookies.txt` para usar a qualidade do YouTube Music Premium. |
| `UPDATE_YTDLP` | Atualiza o yt-dlp a cada arranque (`1`). Resolve a maioria das falhas de download. |

## 6. Manutenção

```bash
docker compose logs -f              # ver o que se passa
docker compose up -d --build --pull always   # atualizar spotDL e as dependências
```

- Se puseres ficheiros manualmente na pasta, clica em **Atualizar biblioteca** no topo da app.
- Se um download falhar, abre o **Registo** no cartão desse download. Na maioria dos casos basta reiniciar o container, para atualizar o yt-dlp, e tentar outra vez.
- Cópia de segurança: a pasta `./data` guarda as playlists, os gostos e o histórico. A música está na tua pasta de música.

## Atalhos

- `Espaço` → reproduzir/pausar
- `Alt + ←/→` → faixa anterior/seguinte
- Duplo clique numa faixa → tocar
- Botão direito numa faixa → menu (fila, playlist, gosto, ir para o álbum/artista)

## Estrutura

```
app/
  main.py        API (biblioteca, streaming com Range, playlists, downloads, login)
  scanner.py     lê tags e capas com mutagen → SQLite
  downloader.py  fila de downloads do spotDL (um de cada vez)
  db.py, config.py
  static/        interface (HTML/CSS/JS sem build), service worker, ícones
```
