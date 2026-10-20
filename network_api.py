from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class NetworkMessage:
    name: str
    data: dict[str, Any]
    sender_id: str | None = None


@dataclass(frozen=True)
class MessageSpec:
    schema: dict[str, Any]
    relay: bool = True


class NetworkAPI:
    """Typed message layer over the project's raw GameAPI.

    Register message types once, then use send() and receive(). The host validates
    incoming messages and relays registered relay-enabled messages to all peers.
    Payloads are JSON envelopes; the underlying GameAPI remains transport-only.
    """

    VERSION = 1

    def __init__(self, raw_api):
        self.raw = raw_api
        self.role = getattr(raw_api, "role", "client")
        self._specs: dict[str, MessageSpec] = {}
        self._handlers: dict[str, list[Callable[[NetworkMessage], None]]] = {}
        self._incoming: list[NetworkMessage] = []
        self._seen_errors = 0

    def register_message(self, name: str, schema: dict[str, Any], *, relay=True):
        """Register fields and expected Python types.

        Example: register_message('player_state', {'id': str, 'x': (int, float)})
        A schema value can also be a callable validator returning bool.
        Fields not listed in the schema are rejected to catch protocol mistakes.
        """
        if not name or not isinstance(name, str):
            raise ValueError("message name must be a non-empty string")
        if name in self._specs:
            if self._specs[name] != MessageSpec(schema, relay):
                raise ValueError(f"message {name!r} is already registered differently")
            return
        self._specs[name] = MessageSpec(dict(schema), bool(relay))
        self._handlers.setdefault(name, [])

    def on(self, name: str, callback: Callable[[NetworkMessage], None]):
        if name not in self._specs:
            raise KeyError(f"register message {name!r} before attaching a handler")
        self._handlers[name].append(callback)

    def send(self, name: str, **data):
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unregistered network message: {name}")
        self._validate(name, data)
        envelope = {
            "_network_api": self.VERSION,
            "type": name,
            "data": data,
        }
        payload = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if self.role == "host":
            self.raw.send_to_all(payload)
            # Local host also consumes its own message so one code path handles all players.
            self._dispatch(NetworkMessage(name, dict(data), "host"))
        else:
            self.raw.send(payload)

    def send_to(self, peer_id, name: str, **data):
        """Host-only targeted message, useful for sending a joining client the roster."""
        if self.role != "host":
            raise RuntimeError("send_to is only available on the host")
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"unregistered network message: {name}")
        self._validate(name, data)
        envelope = {"_network_api": self.VERSION, "type": name, "data": data}
        payload = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.raw.send_to(peer_id, payload)

    def receive(self, timeout=0):
        """Poll the transport and return the next typed message, or None."""
        self._pump(timeout)
        if self._incoming:
            return self._incoming.pop(0)
        return None

    def update(self, timeout=0):
        """Drain transport events and invoke registered callbacks."""
        self._pump(timeout)
        messages, self._incoming = self._incoming, []
        for message in messages:
            for callback in tuple(self._handlers.get(message.name, ())):
                callback(message)
        return len(messages)

    def connected_peers(self):
        method = getattr(self.raw, "connected_peers", None)
        if callable(method):
            try:
                peers = method()
                if peers is not None:
                    return peers
            except (AttributeError, RuntimeError):
                pass

        # Compatibility with this project's HostNetwork, which exposes
        # _peer_snapshot() rather than connected_peers().
        host = getattr(self.raw, "_host", None)
        snapshot = getattr(host, "_peer_snapshot", None)
        if callable(snapshot):
            try:
                return snapshot()
            except (AttributeError, RuntimeError):
                pass
        return []

    def _pump(self, timeout=0):
        first = True
        while True:
            try:
                event = self.raw.receive_event(timeout=timeout if first else 0)
            except (OSError, RuntimeError, AttributeError):
                return
            first = False
            if event is None:
                return
            if getattr(event, "kind", None) != "data_received":
                continue

            try:
                envelope = json.loads(event.data.decode("utf-8"))
                if not isinstance(envelope, dict) or envelope.get("_network_api") != self.VERSION:
                    continue
                name = envelope.get("type")
                data = envelope.get("data")
                if not isinstance(name, str) or not isinstance(data, dict):
                    continue
                spec = self._specs.get(name)
                if spec is None:
                    continue
                self._validate(name, data)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
                self._seen_errors += 1
                continue

            sender = getattr(event, "peer_id", None)
            sender = str(sender) if sender is not None else None

            # Relay validated messages at the host. This is deliberately done
            # before dispatch so every connected client gets the same event.
            if self.role == "host" and spec.relay:
                self._relay(envelope, sender)

            message = NetworkMessage(name, data, sender)
            self._dispatch(message)

    def _dispatch(self, message):
        self._incoming.append(message)

    def _relay(self, envelope, sender_id):
        try:
            payload = json.dumps(envelope, separators=(",", ":"), allow_nan=False).encode("utf-8")
            for peer in self.connected_peers():
                # GameAPI.connected_peers() returns Peer objects in this project.
                # Accept strings too, in case the transport adapter returns IDs.
                peer_id = peer if isinstance(peer, str) else getattr(peer, "peer_id", None)
                if peer_id is None:
                    continue
                # Relay to sender too; its own ID lets gameplay ignore echoes.
                self.raw.send_to(str(peer_id), payload)
        except (OSError, RuntimeError, AttributeError, TypeError, ValueError):
            pass

    def _validate(self, name, data):
        spec = self._specs[name]
        if set(data) != set(spec.schema):
            missing = set(spec.schema) - set(data)
            extra = set(data) - set(spec.schema)
            raise ValueError(f"invalid {name} fields; missing={sorted(missing)}, extra={sorted(extra)}")
        for field, expected in spec.schema.items():
            value = data[field]
            if isinstance(expected, tuple) and all(isinstance(t, type) for t in expected):
                valid = isinstance(value, expected) and not (isinstance(value, bool) and bool not in expected)
            elif isinstance(expected, type):
                valid = isinstance(value, expected) and not (expected is int and isinstance(value, bool))
            elif callable(expected):
                valid = bool(expected(value))
            else:
                raise TypeError(f"unsupported schema for {name}.{field}")
            if not valid:
                raise ValueError(f"invalid type/value for {name}.{field}: {type(value).__name__}")

    @staticmethod
    def number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    @staticmethod
    def color(value):
        return (
            isinstance(value, list) and len(value) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in value)
        )
