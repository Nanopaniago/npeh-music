"""Contas de utilizador do nPlay.

A biblioteca de música e os downloads são partilhados por todos. Cada pessoa tem os seus próprios
gostos, playlists, histórico, estatísticas, músicas identificadas e dispositivos do controlo remoto.

O utilizador atual de cada pedido fica numa ContextVar (db.CURRENT_UID) e as consultas SQL usam a
função uid(), por isso o resto do código não precisa de passar o utilizador de mão em mão.
A conta 1 é o administrador (a conta que já existia, com a palavra-passe do .env).
"""
import hashlib
import hmac
import re
import secrets
import time

from .config import APP_PASSWORD, SECRET_KEY
from .db import CURRENT_UID, get_conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    pw_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0,
    color TEXT,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    last_login INTEGER
);
"""
COLORS = ["#c8ff2e", "#f037a5", "#9bf0e1", "#ff4632", "#7f3aef", "#4100f5"]
RE_USER = re.compile(r"^[a-z0-9._-]{2,32}$")
TOKEN_DAYS = 180


def init():
    conn = get_conn()
    conn.executescript(SCHEMA)
    if not conn.execute("SELECT 1 FROM users").fetchone():
        # a conta que já existia passa a ser o administrador
        conn.execute("INSERT INTO users(id, username, name, pw_hash, is_admin, color) VALUES (1, 'nano', 'Nano', ?, 1, ?)",
                     (hash_pw(APP_PASSWORD) if APP_PASSWORD else None, COLORS[0]))
    conn.commit()


# ---------------------------------------------------------------- palavras-passe
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000)
    return f"pbkdf2${salt}${dk.hex()}"


def check_pw(pw: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 240_000)
    return hmac.compare_digest(dk.hex(), digest)


def auth_enabled() -> bool:
    return bool(get_conn().execute("SELECT 1 FROM users WHERE pw_hash IS NOT NULL").fetchone())


# ---------------------------------------------------------------- sessões (cookie assinado)
def _sig(uid, exp, pw_hash):
    # mudar a palavra-passe invalida as sessões antigas desse utilizador
    return hmac.new(SECRET_KEY.encode(), f"{uid}.{exp}.{(pw_hash or '')[-24:]}".encode(), hashlib.sha256).hexdigest()[:40]


def make_token(user) -> str:
    exp = int(time.time()) + TOKEN_DAYS * 86400
    return f"{user['id']}.{exp}.{_sig(user['id'], exp, user['pw_hash'])}"


def from_token(token: str | None):
    if not auth_enabled():
        return get(1)
    try:
        uid, exp, sig = (token or "").split(".")
        uid, exp = int(uid), int(exp)
    except ValueError:
        return None
    if exp < time.time():
        return None
    u = get(uid, raw=True)
    if not u or not hmac.compare_digest(sig, _sig(uid, exp, u["pw_hash"])):
        return None
    return public(u)


# ---------------------------------------------------------------- consultas
def public(u):
    if not u:
        return None
    return {"id": u["id"], "username": u["username"], "name": u["name"], "is_admin": bool(u["is_admin"]),
            "color": u["color"], "has_password": bool(u["pw_hash"]), "created_at": u["created_at"], "last_login": u["last_login"]}


def get(uid, raw=False):
    r = get_conn().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return (dict(r) if raw else public(r)) if r else None


def login(username: str, password: str):
    conn = get_conn()
    username = (username or "").strip()
    if username:
        u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    else:   # compatibilidade: só a palavra-passe entra na conta de administrador
        u = conn.execute("SELECT * FROM users WHERE id=1").fetchone()
    if not u or not check_pw(password or "", u["pw_hash"]):
        return None
    conn.execute("UPDATE users SET last_login=strftime('%s','now') WHERE id=?", (u["id"],))
    conn.commit()
    return dict(u)


def listing():
    conn = get_conn()
    out = []
    for r in conn.execute("SELECT * FROM users ORDER BY id"):
        u = public(r)
        u["playlists"] = conn.execute("SELECT COUNT(*) FROM playlists WHERE owner_id=?", (r["id"],)).fetchone()[0]
        u["likes"] = conn.execute("SELECT COUNT(*) FROM likes WHERE user_id=?", (r["id"],)).fetchone()[0]
        u["plays"] = conn.execute("SELECT COUNT(*) FROM plays WHERE user_id=?", (r["id"],)).fetchone()[0]
        out.append(u)
    return out


class UserError(ValueError):
    pass


def create(username, name, password, is_admin=False):
    username = (username or "").strip().lower()
    if not RE_USER.match(username):
        raise UserError("O nome de utilizador deve ter 2 a 32 letras minúsculas, números, ponto, hífen ou _.")
    if len(password or "") < 6:
        raise UserError("A palavra-passe deve ter pelo menos 6 caracteres.")
    conn = get_conn()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise UserError("Já existe um utilizador com esse nome.")
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    cur = conn.execute("INSERT INTO users(username, name, pw_hash, is_admin, color) VALUES (?,?,?,?,?)",
                       (username, (name or username).strip()[:40], hash_pw(password), int(bool(is_admin)), COLORS[n % len(COLORS)]))
    conn.commit()
    return get(cur.lastrowid)


def update(uid, data, by_admin):
    conn = get_conn()
    u = get(uid, raw=True)
    if not u:
        raise UserError("Utilizador não encontrado.")
    if "name" in data and str(data["name"]).strip():
        conn.execute("UPDATE users SET name=? WHERE id=?", (str(data["name"]).strip()[:40], uid))
    if data.get("password"):
        if len(data["password"]) < 6:
            raise UserError("A palavra-passe deve ter pelo menos 6 caracteres.")
        if not by_admin and not check_pw(data.get("current_password", ""), u["pw_hash"]) and u["pw_hash"]:
            raise UserError("A palavra-passe atual está errada.")
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_pw(data["password"]), uid))
    if by_admin and "is_admin" in data and uid != 1:
        conn.execute("UPDATE users SET is_admin=? WHERE id=?", (int(bool(data["is_admin"])), uid))
    conn.commit()
    return get(uid)


def delete(uid):
    if uid == 1:
        raise UserError("A conta principal não pode ser apagada.")
    conn = get_conn()
    conn.execute("DELETE FROM likes WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM plays WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM playlists WHERE owner_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()


def current_id():
    return CURRENT_UID.get()
