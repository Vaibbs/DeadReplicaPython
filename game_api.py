"""
Gameplay-facing API for Dead Replica.

Import this from gameplay code; keep UI/game simulation in other modules.

Host:
    api.send_to(peer_id, b"...")
    api.send_to_all(b"...")
    api.connected_peers()
    event = api.receive_event()

Client:
    api.send(b"...")
    event = api.receive_event()

`GameAPI` is a small adapter that can be connected to the host/client network
objects in main.py. It intentionally does not define a game protocol or state
schema. Bytes passed through the API are opaque application payloads.
"""
from __future__ import annotations

import queue
from dataclasses import dataclass
from typing import Optional


@dataclass
class NetworkEvent:
    kind: str
    peer_id: str = ""
    data: bytes = b""
    address: tuple[str, int] | None = None
    message: str = ""


class GameAPI:
    """Stable names for gameplay code; bind a transport with attach_host/client."""

    def __init__(self, role: str):
        if role not in ("host", "client"):
            raise ValueError("role must be 'host' or 'client'")
        self.role = role
        self._host = None
        self._client = None
        self.events: queue.Queue[NetworkEvent] = queue.Queue()
        self._closed = False

    def attach_host(self, host_network) -> None:
        if self.role != "host":
            raise RuntimeError("Only host GameAPI can attach a host network")
        self._host = host_network

    def attach_client(self, client_network) -> None:
        if self.role != "client":
            raise RuntimeError("Only client GameAPI can attach a client network")
        self._client = client_network

    def send_to(self, peer_id: str, data: bytes) -> bool:
        if self.role != "host" or self._host is None:
            raise RuntimeError("send_to is available only to an attached host")
        return self._host.send_to(peer_id, data)

    def send_to_all(self, data: bytes, exclude_peer_ids: set[str] | None = None) -> int:
        if self.role != "host" or self._host is None:
            raise RuntimeError("send_to_all is available only to an attached host")
        return self._host.send_to_all(data, exclude_peer_ids)

    def connected_peers(self) -> list[dict]:
        if self.role != "host" or self._host is None:
            return []
        return self._host.connected_peers()

    def send(self, data: bytes) -> bool:
        if self.role != "client" or self._client is None:
            raise RuntimeError("send is available only to an attached client")
        return self._client.send(data)

    def receive_event(self, timeout: float | None = 0) -> NetworkEvent | None:
        """Return next gameplay data event, or None if no event is ready."""
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def _record_received(self, peer_id: str, data: bytes) -> None:
        self.events.put(NetworkEvent("data_received", peer_id=peer_id, data=data))

    def close(self) -> None:
        self._closed = True
