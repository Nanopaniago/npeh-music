"""Prioridades do servidor.

1. Música + capa (a fila de downloads) — tem sempre a vez.
2. Letras e vídeos curtos — só quando não há músicas a descarregar.
3. O resto (géneros, lançamentos, sincronizar playlists, releitura da pasta) — só com o servidor livre.

O downloader atualiza `state`; as tarefas de fundo chamam `wait_idle()` (threads) ou `busy()` (ciclos asyncio)
antes de cada passo, por isso cedem a vez assim que entra um download novo.
"""
import time

state = {"running": None, "queued": 0, "since": 0.0}


def busy() -> bool:
    return bool(state["running"]) or state["queued"] > 0


def idle_for() -> float:
    """Segundos desde que a fila ficou vazia (0 se está ocupada)."""
    return 0.0 if busy() else time.time() - state["since"]


def wait_idle(settle: float = 0, poll: float = 5, max_wait: float | None = None) -> bool:
    """Bloqueia (numa thread) até não haver downloads; `settle` = segundos de folga depois de esvaziar.
    -> True quando livre, False se passou `max_wait`."""
    t0 = time.time()
    while busy() or idle_for() < settle:
        if max_wait is not None and time.time() - t0 > max_wait:
            return False
        time.sleep(poll)
    return True
