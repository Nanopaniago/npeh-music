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
        "load", "enqueue", "transfer", "stop"}


class Hub:
    def __init__(self):
        self.devices = {}  # id -> {id, name, kind, ws, state, at}

    def _payload(self):
        now = time.time()
        return {"t": "devices", "list": [
            {"id": d["id"], "name": d["name"], "kind": d["kind"], "state": d["state"],
             "age": round(now - d["at"], 2) if d["state"] else 0}
            for d in self.devices.values()]}

    async def _send(self, dev, msg):
        try:
            await dev["ws"].send_json(msg)
        except Exception:  # noqa
            self.devices.pop(dev["id"], None) if self.devices.get(dev["id"]) is dev else None

    async def broadcast(self):
        msg = self._payload()
        await asyncio.gather(*[self._send(d, msg) for d in list(self.devices.values())], return_exceptions=True)

    async def serve(self, ws: WebSocket):
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
                    did = str(msg.get("id") or "")[:64]
                    if not did:
                        continue
                    old = self.devices.get(did)
                    dev = {"id": did, "name": str(msg.get("name") or "Dispositivo")[:40],
                           "kind": msg.get("kind") if msg.get("kind") in ("desktop", "phone", "tablet") else "desktop",
                           "ws": ws, "state": (old or {}).get("state"), "at": (old or {}).get("at", time.time())}
                    self.devices[did] = dev
                    if old and old["ws"] is not ws:
                        try:
                            await old["ws"].close()
                        except Exception:  # noqa
                            pass
                    await self.broadcast()
                elif dev is None:
                    continue
                elif t == "state":
                    st = msg.get("state")
                    dev["state"] = st if isinstance(st, dict) else None
                    dev["at"] = time.time()
                    await self.broadcast()
                elif t == "rename":
                    dev["name"] = str(msg.get("name") or dev["name"])[:40]
                    await self.broadcast()
                elif t == "cmd":
                    target = self.devices.get(str(msg.get("to") or ""))
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
            if dev and self.devices.get(dev["id"], {}).get("ws") is ws:
                self.devices.pop(dev["id"], None)
                await self.broadcast()


hub = Hub()
