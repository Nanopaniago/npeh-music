"""Controlo remoto entre dispositivos (ao estilo Spotify Connect) através de WebSocket.

Cada separador/dispositivo liga-se a /api/ws, anuncia-se (hello) e vai enviando o seu estado de
reprodução. Qualquer dispositivo pode enviar comandos a outro: o servidor só reencaminha.
Tudo em memória, num único processo.
"""
import asyncio
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("remote")
CMDS = {"toggle", "play", "pause", "next", "prev", "seek", "volume", "shuffle", "repeat",
        "load", "enqueue", "transfer", "stop", "disconnect"}


class Hub:
    def __init__(self):
        self.devices = {}  # id -> {id, name, kind, ws, state, at}

    # cada utilizador só vê e controla os seus próprios dispositivos
    def _payload(self, uid):
        now = time.time()
        return {"t": "devices", "list": [
            {"id": d["id"], "name": d["name"], "kind": d["kind"], "state": d["state"],
             "age": round(now - d["at"], 2) if d["state"] else 0}
            for d in self.devices.values() if d["uid"] == uid]}

    async def _send(self, dev, msg):
        try:
            await dev["ws"].send_json(msg)
        except Exception:  # noqa
            self.devices.pop(dev["key"], None) if self.devices.get(dev["key"]) is dev else None

    async def broadcast(self, uid=None):
        devs = list(self.devices.values())
        uids = {uid} if uid is not None else {d["uid"] for d in devs}
        await asyncio.gather(*[self._send(d, self._payload(d["uid"])) for d in devs if d["uid"] in uids], return_exceptions=True)

    async def serve(self, ws: WebSocket, uid: int = 1):
        dev = None
        try:
            while True:
                try:
                    msg = await ws.receive_json()
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                t = msg.get("t")
                if t == "hello":
                    raw = str(msg.get("id") or "")[:64]
                    if not raw:
                        continue
                    did = f"{uid}:{raw}"          # o mesmo browser pode entrar com contas diferentes
                    old = self.devices.get(did)
                    dev = {"id": raw, "key": did, "name": str(msg.get("name") or "Dispositivo")[:40],
                           "kind": msg.get("kind") if msg.get("kind") in ("desktop", "phone", "tablet") else "desktop",
                           "ws": ws, "uid": uid, "state": (old or {}).get("state"), "at": (old or {}).get("at", time.time())}
                    self.devices[did] = dev
                    if old and old["ws"] is not ws:
                        try:
                            await old["ws"].close()
                        except Exception:  # noqa
                            pass
                    await self.broadcast(uid)
                elif dev is None:
                    continue
                elif t == "state":
                    st = msg.get("state")
                    dev["state"] = st if isinstance(st, dict) else None
                    dev["at"] = time.time()
                    await self.broadcast(uid)
                elif t == "rename":
                    dev["name"] = str(msg.get("name") or dev["name"])[:40]
                    await self.broadcast(uid)
                elif t == "cmd":
                    target = self.devices.get(f'{uid}:{str(msg.get("to") or "")}')
                    cmd = msg.get("cmd")
                    if target and cmd in CMDS:
                        await self._send(target, {"t": "cmd", "from": dev["id"], "cmd": cmd,
                                                  "args": msg.get("args") if isinstance(msg.get("args"), dict) else {}})
                elif t == "ping":
                    await ws.send_json({"t": "pong"})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa
            log.debug("ws terminou", exc_info=True)
        finally:
            if dev and self.devices.get(dev["key"], {}).get("ws") is ws:
                self.devices.pop(dev["key"], None)
                await self.broadcast(uid)


hub = Hub()
