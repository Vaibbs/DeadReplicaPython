"""
Dead Replica launcher, lobby UI, and synchronized scene transition.

Python 3.10+
    python -m pip install -r requirements.txt
    python main.py

Gameplay is intentionally kept out of this file. See game_api.py.
TCP protocol is internal infrastructure only: length-prefixed UTF-8 JSON control
messages are used for lobby/snapshot/scene synchronization. Gameplay payloads are
opaque bytes carried by the separate GameAPI channel.
"""
from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import pygame

from game_api import GameAPI, NetworkEvent
from gameplay import GameplayScene

APP_NAME = "DEAD REPLICA"
DEFAULT_PORT = 7777
DISCOVERY_PORT = 7778
MAX_PLAYERS = 4
PROTOCOL_VERSION = 1

BG = (18, 25, 29)
PANEL = (29, 39, 43)
PANEL_LIGHT = (39, 52, 56)
ACCENT = (143, 198, 120)
ACCENT_DARK = (94, 145, 83)
TEXT = (239, 242, 232)
MUTED = (160, 177, 176)
RED = (207, 105, 91)
GOLD = (225, 185, 92)
MAX_FRAME_BYTES = 1_000_000


def _encode_control(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError("Control message exceeds maximum frame size")
    return struct.pack("!I", len(payload)) + payload


def _recv_exact(sock: socket.socket, size: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < size:
        try:
            chunk = sock.recv(size - len(chunks))
        except socket.timeout:
            continue
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def _read_control(sock: socket.socket) -> dict[str, Any] | None:
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ValueError(f"Invalid control frame size: {size}")
    payload = _recv_exact(sock, size)
    if payload is None:
        return None
    obj = json.loads(payload.decode("utf-8"))
    if not isinstance(obj, dict):
        raise ValueError("Control frame must be a JSON object")
    return obj


@dataclass
class Peer:
    peer_id: str
    name: str
    address: tuple[str, int]
    sock: socket.socket = field(repr=False)
    send_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class HostNetwork:
    """Host-side lobby/control server. Gameplay bytes are delegated to GameAPI."""

    def __init__(self, port: int = DEFAULT_PORT, max_players: int = MAX_PLAYERS):
        self.port = port
        self.max_players = max(1, min(MAX_PLAYERS, max_players))
        self.host_id = "host"
        self.peers: dict[str, Peer] = {}
        self.lock = threading.RLock()
        self.events: queue.Queue[NetworkEvent] = queue.Queue()
        self.running = False
        self.server: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.discovery_thread: threading.Thread | None = None
        self.game_started = False
        self.api = GameAPI(role="host")
        self.api.attach_host(self)

    def start(self) -> None:
        if self.running:
            return
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", self.port))
        server.listen(self.max_players - 1 if self.max_players > 1 else 1)
        server.settimeout(0.5)
        self.server = server
        self.running = True
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()
        self.discovery_thread = threading.Thread(target=self._discovery_loop, daemon=True)
        self.discovery_thread.start()
        self.events.put(NetworkEvent("server_started", message=f"TCP {self.port}"))

    def _accept_loop(self) -> None:
        while self.running and self.server:
            try:
                conn, addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.settimeout(0.5)
            with self.lock:
                if len(self.peers) + 1 >= self.max_players or self.game_started:
                    try:
                        self._send_control_socket(conn, {"type": "REJECT", "reason":
                            "Lobby full" if not self.game_started else "Game already started"})
                    except OSError:
                        pass
                    conn.close()
                    continue
            peer_id = uuid.uuid4().hex[:8]
            peer = Peer(peer_id, f"Player-{peer_id[:4]}", (addr[0], addr[1]), conn)
            with self.lock:
                self.peers[peer_id] = peer
            thread = threading.Thread(target=self._peer_loop, args=(peer,), daemon=True)
            thread.start()

    @staticmethod
    def _send_control_socket(sock: socket.socket, message: dict[str, Any]) -> None:
        data = _encode_control(message)
        sock.sendall(data)

    def _send_control(self, peer: Peer, message: dict[str, Any]) -> bool:
        try:
            data = _encode_control(message)
            with peer.send_lock:
                peer.sock.sendall(data)
            return True
        except (OSError, ValueError):
            self._drop_peer(peer.peer_id, "Control send failed")
            return False

    def _peer_loop(self, peer: Peer) -> None:
        welcomed = False
        try:
            # First control frame must be HELLO.
            hello = _read_control(peer.sock)
            if not hello or hello.get("type") != "HELLO" or hello.get("version") != PROTOCOL_VERSION:
                self._send_control(peer, {"type": "REJECT", "reason": "Protocol mismatch"})
                return
            name = str(hello.get("name", "Player"))[:20].strip() or "Player"
            peer.name = name
            welcomed = True
            self._send_control(peer, {
                "type": "WELCOME", "version": PROTOCOL_VERSION,
                "peer_id": peer.peer_id, "host_id": self.host_id,
                "scene": "game" if self.game_started else "lobby",
                "players": self._players_snapshot(),
                "max_players": self.max_players,
            })
            self._broadcast_players()
            self.events.put(NetworkEvent("peer_connected", peer.peer_id,
                                         address=peer.address, message=peer.name))
            while self.running:
                message = _read_control(peer.sock)
                if message is None:
                    break
                kind = message.get("type")
                if kind == "PING":
                    self._send_control(peer, {"type": "PONG", "time": time.time()})
                elif kind == "SET_NAME":
                    new_name = str(message.get("name", "Player"))[:20].strip()
                    if new_name:
                        peer.name = new_name
                        self._broadcast_players()
                elif kind == "GAME_DATA":
                    payload = message.get("data")
                    if isinstance(payload, str):
                        try:
                            raw = bytes.fromhex(payload)
                            self.events.put(NetworkEvent("data_received", peer.peer_id,
                                                         data=raw, address=peer.address))
                            self.api._record_received(peer.peer_id, raw)
                        except ValueError:
                            self._send_control(peer, {"type": "ERROR", "message": "Invalid data encoding"})
                else:
                    self.events.put(NetworkEvent("control_received", peer.peer_id,
                                                 message=json.dumps(message)))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.events.put(NetworkEvent("network_error", peer.peer_id, message=str(exc)))
        finally:
            self._drop_peer(peer.peer_id, "Disconnected", broadcast=welcomed)

    def _players_snapshot(self) -> list[dict[str, str]]:
        with self.lock:
            result = [{"peer_id": self.host_id, "name": "Host", "is_host": True}]
            result.extend({"peer_id": p.peer_id, "name": p.name, "is_host": False}
                          for p in self.peers.values())
            return result

    def _broadcast_players(self) -> None:
        message = {"type": "PLAYERS", "players": self._players_snapshot()}
        for peer in self._peer_snapshot():
            self._send_control(peer, message)
        self.events.put(NetworkEvent("players_updated", message=json.dumps(message["players"])))

    def _peer_snapshot(self) -> list[Peer]:
        with self.lock:
            return list(self.peers.values())

    def _drop_peer(self, peer_id: str, reason: str, broadcast: bool = True) -> None:
        with self.lock:
            peer = self.peers.pop(peer_id, None)
        if not peer:
            return
        try:
            peer.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            peer.sock.close()
        except OSError:
            pass
        self.events.put(NetworkEvent("peer_disconnected", peer_id,
                                     address=peer.address, message=reason))
        if broadcast and self.running:
            self._broadcast_players()

    def send_to(self, peer_id: str, data: bytes) -> bool:
        """Public gameplay API: send opaque bytes to one client."""
        peer = next((p for p in self._peer_snapshot() if p.peer_id == peer_id), None)
        if not peer:
            return False
        return self._send_control(peer, {"type": "GAME_DATA", "data": bytes(data).hex()})

    def send_to_all(self, data: bytes, exclude_peer_ids: set[str] | None = None) -> int:
        """Public gameplay API: broadcast opaque bytes to connected clients."""
        excluded = exclude_peer_ids or set()
        return sum(1 for peer in self._peer_snapshot()
                   if peer.peer_id not in excluded and self._send_control(
                       peer, {"type": "GAME_DATA", "data": bytes(data).hex()}))

    def start_game(self) -> None:
        with self.lock:
            self.game_started = True
        self.send_control_to_all({"type": "START_GAME", "scene": "game", "started_at": time.time()})
        self.events.put(NetworkEvent("game_started", message="Host started the game"))

    def send_control_to_all(self, message: dict[str, Any]) -> None:
        for peer in self._peer_snapshot():
            self._send_control(peer, message)

    def disconnect_peer(self, peer_id: str) -> None:
        self._drop_peer(peer_id, "Removed by host")

    def _discovery_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", DISCOVERY_PORT))
            sock.settimeout(0.5)
            while self.running:
                try:
                    data, addr = sock.recvfrom(1024)
                    if data == b"DEADREPLICA_DISCOVER":
                        reply = json.dumps({
                            "type": "DEADREPLICA_HOST",
                            "port": self.port,
                            "players": len(self._players_snapshot()),
                            "max_players": self.max_players,
                        }).encode("utf-8")
                        sock.sendto(reply, addr)
                except socket.timeout:
                    continue
                except OSError:
                    break
        finally:
            sock.close()

    def close(self) -> None:
        self.running = False
        try:
            if self.server:
                self.server.close()
        except OSError:
            pass
        for peer in self._peer_snapshot():
            self._drop_peer(peer.peer_id, "Host stopped", broadcast=False)
        self.api.close()
        self.server = None


class ClientNetwork:
    def __init__(self):
        self.sock: socket.socket | None = None
        self.running = False
        self.events: queue.Queue[NetworkEvent] = queue.Queue()
        self.reader: threading.Thread | None = None
        self.send_lock = threading.Lock()
        self.peer_id = ""
        self.players: list[dict[str, Any]] = []
        self.host_address: tuple[str, int] | None = None
        self.api = GameAPI(role="client")
        self.api.attach_client(self)

    def connect(self, host: str, port: int, name: str = "Player", timeout: float = 4.0) -> None:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.settimeout(0.5)
        self.sock = sock
        self.host_address = (host, port)
        self.running = True
        self._send_control({"type": "HELLO", "version": PROTOCOL_VERSION, "name": name})
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _send_control(self, message: dict[str, Any]) -> bool:
        if not self.sock:
            return False
        try:
            data = _encode_control(message)
            with self.send_lock:
                self.sock.sendall(data)
            return True
        except (OSError, ValueError):
            return False

    def _read_loop(self) -> None:
        try:
            while self.running and self.sock:
                message = _read_control(self.sock)
                if message is None:
                    break
                kind = message.get("type")
                if kind == "WELCOME":
                    self.peer_id = str(message.get("peer_id", ""))
                    self.players = message.get("players", [])
                    self.events.put(NetworkEvent("welcome", message=json.dumps(message)))
                elif kind == "PLAYERS":
                    self.players = message.get("players", [])
                    self.events.put(NetworkEvent("players_updated", message=json.dumps(self.players)))
                elif kind == "START_GAME":
                    self.events.put(NetworkEvent("start_game", message=json.dumps(message)))
                elif kind == "GAME_DATA":
                    try:
                        raw = bytes.fromhex(message.get("data", ""))
                        self.api._record_received("host", raw)
                        self.events.put(NetworkEvent("data_received", data=raw))
                    except ValueError:
                        self.events.put(NetworkEvent("network_error", message="Invalid GAME_DATA"))
                elif kind == "REJECT":
                    self.events.put(NetworkEvent("rejected", message=str(message.get("reason", "Rejected"))))
                    break
                elif kind == "PING":
                    self._send_control({"type": "PONG", "time": time.time()})
                else:
                    self.events.put(NetworkEvent("control_received", message=json.dumps(message)))
        except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.events.put(NetworkEvent("network_error", message=str(exc)))
        finally:
            was_running = self.running
            self.running = False
            if was_running:
                self.events.put(NetworkEvent("disconnected", message="Host connection closed"))

    def send(self, data: bytes) -> bool:
        """Public gameplay API: send opaque bytes to the host."""
        return self._send_control({"type": "GAME_DATA", "data": bytes(data).hex()})

    def close(self) -> None:
        self.running = False
        sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        self.api.close()


def discover_hosts(timeout: float = 2.5) -> list[tuple[str, int, int, int]]:
    found: dict[tuple[str, int], tuple[str, int, int, int]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.2)
        sock.sendto(b"DEADREPLICA_DISCOVER", ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
                obj = json.loads(data.decode("utf-8"))
                if obj.get("type") == "DEADREPLICA_HOST":
                    item = (addr[0], int(obj.get("port", DEFAULT_PORT)),
                            int(obj.get("players", 1)), int(obj.get("max_players", MAX_PLAYERS)))
                    found[(item[0], item[1])] = item
            except socket.timeout:
                pass
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                pass
    finally:
        sock.close()
    return sorted(found.values())


class Button:
    def __init__(self, rect, text, callback, color=ACCENT, text_color=BG):
        self.rect = pygame.Rect(rect)
        self.text, self.callback = text, callback
        self.color, self.text_color = color, text_color
        self.hover = False

    def draw(self, surface, font):
        color = tuple(min(255, int(c * (1.07 if self.hover else 1))) for c in self.color)
        pygame.draw.rect(surface, color, self.rect, border_radius=10)
        label = font.render(self.text, True, self.text_color)
        surface.blit(label, label.get_rect(center=self.rect.center))

    def handle(self, event):
        if event.type == pygame.MOUSEMOTION:
            self.hover = self.rect.collidepoint(event.pos)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1 and self.rect.collidepoint(event.pos):
            self.callback()


class DeadReplicaApp:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption(APP_NAME)
        self.screen = pygame.display.set_mode((1100, 720), pygame.RESIZABLE)
        self.clock = pygame.time.Clock()
        self.title_font = pygame.font.SysFont("arial", 44, bold=True)
        self.heading_font = pygame.font.SysFont("arial", 25, bold=True)
        self.font = pygame.font.SysFont("arial", 19)
        self.small_font = pygame.font.SysFont("arial", 15)
        self.running = True
        self.scene = "menu"
        self.status = "Ready"
        self.host: HostNetwork | None = None
        self.client: ClientNetwork | None = None
        self.name = socket.gethostname()[:20] or "Player"
        self.input_text = ""
        self.input_active = False
        self.searching = False
        self.lan_hosts: list[tuple[str, int, int, int]] = []
        self.lobby_players: list[dict[str, Any]] = []
        self.peer_id = "host"
        self.gameplay_scene: GameplayScene | None = None
        self.buttons: list[Button] = []
        self._layout()

    def size(self):
        return self.screen.get_size()

    def _layout(self):
        w, h = self.size()
        cx = w // 2
        self.buttons = []
        if self.scene == "menu":
            self.buttons = [
                Button((cx-150, int(h*.48), 300, 58), "HOST GAME", self.host_game),
                Button((cx-150, int(h*.60), 300, 58), "JOIN GAME", self.open_join, PANEL_LIGHT, TEXT),
                Button((cx-150, int(h*.72), 300, 48), "QUIT", self.quit, (64, 76, 79), TEXT),
            ]
        elif self.scene == "join":
            self.buttons = [
                Button((cx-300, int(h*.48), 185, 48), "SEARCH LAN", self.search_lan),
                Button((cx+115, int(h*.48), 185, 48), "CONNECT", self.direct_connect, PANEL_LIGHT, TEXT),
                Button((cx-100, int(h*.78), 200, 48), "BACK", self.go_menu, PANEL_LIGHT, TEXT),
            ]
        elif self.scene == "lobby":
            if self.host:
                self.buttons = [
                    Button((cx-250, int(h*.78), 210, 52), "LEAVE LOBBY", self.leave_lobby, PANEL_LIGHT, TEXT),
                    Button((cx+40, int(h*.78), 210, 52), "START GAME", self.start_game),
                ]
            else:
                self.buttons = [Button((cx-100, int(h*.78), 200, 52), "LEAVE LOBBY", self.leave_lobby, PANEL_LIGHT, TEXT)]
        elif self.scene == "game":
            self.buttons = []

    def _background(self):
        w, h = self.size()
        self.screen.fill(BG)
        import random
        random.seed(19)
        for _ in range(max(120, (w*h)//4200)):
            pygame.draw.circle(self.screen, random.choice([(24,36,36),(25,34,31),(30,41,37),(34,45,39)]),
                               (random.randrange(w), random.randrange(h)), random.choice([1,1,2]))
        pygame.draw.rect(self.screen, ACCENT_DARK, (0, 0, w, 5))
        pygame.draw.circle(self.screen, (32,47,45), (w-40, 70), 95, 1)
        pygame.draw.circle(self.screen, (32,47,45), (w-40, 70), 72, 1)

    def _panel(self, rect, color=PANEL):
        surf = pygame.Surface((rect[2], rect[3]), pygame.SRCALPHA)
        pygame.draw.rect(surf, (*color, 244), surf.get_rect(), border_radius=18)
        pygame.draw.rect(surf, (255,255,255,20), surf.get_rect(), 1, border_radius=18)
        self.screen.blit(surf, rect[:2])

    def _text(self, text, pos, font=None, color=TEXT, anchor="topleft"):
        image = (font or self.font).render(str(text), True, color)
        rect = image.get_rect()
        setattr(rect, anchor, pos)
        self.screen.blit(image, rect)

    def draw(self):
        self._background()
        w, h = self.size()
        self._text(APP_NAME, (70, 52), self.title_font, ACCENT)
        self._text("CO-OP SURVIVAL  /  EVOLVING THREATS", (73, 106), self.small_font, MUTED)
        if self.scene == "menu":
            self._panel((max(25,w//2-260),int(h*.29),520,int(h*.55)))
            self._text("SURVIVE TOGETHER.",(w//2,int(h*.36)),self.heading_font,TEXT,"center")
            self._text("Gather. Build. Survive the next wave.",(w//2,int(h*.415)),self.font,MUTED,"center")
        elif self.scene == "join":
            self._panel((max(20,w//2-360),int(h*.25),720,int(h*.58)))
            self._text("JOIN A GAME",(w//2,int(h*.30)),self.heading_font,TEXT,"center")
            self._text("LAN HOSTS",(w//2-300,int(h*.40)),self.small_font,MUTED)
            if self.searching:
                self._text("Searching local network…",(w//2-300,int(h*.44)),self.font,GOLD)
            elif self.lan_hosts:
                for i,(ip,port,players,maxp) in enumerate(self.lan_hosts[:5]):
                    self._text(f"{ip}:{port}   {players}/{maxp} players",(w//2-300,int(h*.44)+i*28),self.font,TEXT)
            else:
                self._text("No hosts found yet. Search LAN or enter an address below.",(w//2-300,int(h*.44)),self.small_font,MUTED)
            self._panel((w//2-300,int(h*.61),600,58),(19,28,31))
            shown = self.input_text or "Click and type host IP, e.g. 192.168.1.10"
            self._text(shown,(w//2-282,int(h*.61)+18),self.font,TEXT if self.input_text else MUTED)
            if self.input_active:
                pygame.draw.rect(self.screen,ACCENT,(w//2-300,int(h*.61),600,58),2,border_radius=10)
            self._text("Click a discovered host row or connect by IP.",(w//2-300,int(h*.70),),self.small_font,MUTED)
        elif self.scene == "lobby":
            self._panel((max(20,w//2-360),int(h*.24),720,int(h*.60)))
            self._text("GAME LOBBY",(w//2,int(h*.30)),self.heading_font,ACCENT,"center")
            self._text("HOST • SERVER" if self.host else "CLIENT • CONNECTED",(w//2,int(h*.36)),self.small_font,MUTED,"center")
            self._text(f"Players: {len(self.lobby_players)}/4",(w//2-300,int(h*.43)),self.font,TEXT)
            for i,p in enumerate(self.lobby_players):
                pid, name = p.get("peer_id","?"), p.get("name","Player")
                self._text(f"●  {name}  [{pid}]",(w//2-280,int(h*.49)+i*29),self.font,ACCENT if pid == self.peer_id else TEXT)
            self._text("Host starts the game; all connected clients switch together.",(w//2,int(h*.70)),self.small_font,MUTED,"center")
        elif self.scene == "game":
            # Gameplay rendering is delegated to its own module.
            if self.gameplay_scene:
                self.gameplay_scene.draw(self.screen)
            else:
                self._panel((max(20,w//2-360),int(h*.25),720,int(h*.45)))
                self._text("GAME SCENE",(w//2,int(h*.32)),self.heading_font,ACCENT,"center")
        for button in self.buttons:
            button.draw(self.screen,self.font)
        self._text(self.status,(24,h-22),self.small_font,MUTED,"bottomleft")
        pygame.display.flip()

    def host_game(self):
        try:
            self.host = HostNetwork(DEFAULT_PORT, MAX_PLAYERS)
            self.host.start()
            self.peer_id = "host"
            self.lobby_players = self.host._players_snapshot()
            self.scene = "lobby"
            self.status = f"Hosting TCP {DEFAULT_PORT} • LAN discovery UDP {DISCOVERY_PORT}"
            self._layout()
        except OSError as exc:
            if self.host:
                self.host.close()
            self.host = None
            self.status = f"Could not host: {exc}"

    def open_join(self):
        self.scene="join"; self.input_text=""; self.input_active=False
        self.lan_hosts=[]; self.searching=False; self.status="Search LAN or enter an IP address"
        self._layout()

    def search_lan(self):
        if self.searching: return
        self.searching=True; self.lan_hosts=[]
        def worker():
            hosts=discover_hosts()
            self.lan_hosts=hosts; self.searching=False
            self.status=f"Found {len(hosts)} host(s)" if hosts else "No host found; try direct connect"
        threading.Thread(target=worker,daemon=True).start()

    def direct_connect(self):
        address=self.input_text.strip()
        if not address:
            self.status="Enter a host IP address first"; return
        self.connect_to(address,DEFAULT_PORT)

    def connect_to(self,address,port):
        try:
            client=ClientNetwork()
            client.connect(address,port,self.name)
            self.client=client
            self.status=f"Connecting to {address}:{port}…"
        except (OSError,ValueError) as exc:
            self.status=f"Connection failed: {exc}"

    def start_game(self):
        if not self.host: return
        self.host.start_game()
        self._enter_game()
        self.status="Game started; connected clients receive the same scene transition"

    def _enter_game(self):
        self.scene = "game"
        api = self.host.api if self.host else (
            self.client.api if self.client else None
        )
        self.gameplay_scene = (
            GameplayScene(api, on_exit=self.back_to_lobby) if api else None
        )
        self.status = "In game"
        self._layout()

    def back_to_lobby(self):
        # This is a local UI navigation only; does not rewind network session.
        self.scene="lobby"; self._layout()

    def leave_lobby(self):
        self.cleanup_network()
        self.scene="menu"; self.status="Left lobby"; self.lobby_players=[]
        self._layout()

    def go_menu(self):
        self.scene="menu"; self.status="Ready"; self._layout()

    def quit(self):
        self.running=False

    def process_network(self):
        if self.host:
            while True:
                try: event=self.host.events.get_nowait()
                except queue.Empty: break
                if event.kind in ("peer_connected","peer_disconnected","players_updated"):
                    self.lobby_players=self.host._players_snapshot()
                    self.status=event.message or event.kind.replace("_"," ").title()
                    self._layout()
                # elif event.kind=="data_received":
                #     print(f"[HOST GAME DATA] from {event.peer_id}: {event.data!r}")
                # elif event.kind=="network_error":
                #     print(f"[HOST NETWORK ERROR] {event.peer_id}: {event.message}")
        if self.client:
            while True:
                try: event=self.client.events.get_nowait()
                except queue.Empty: break
                if event.kind=="welcome":
                    obj=json.loads(event.message)
                    self.peer_id=self.client.peer_id
                    self.lobby_players=obj.get("players",[])
                    self.status="Connected • lobby synchronized"
                    self.scene="game" if obj.get("scene")=="game" else "lobby"
                    self._layout()
                elif event.kind=="players_updated":
                    self.lobby_players=json.loads(event.message)
                    self._layout()
                elif event.kind=="start_game":
                    self._enter_game()
                # elif event.kind=="data_received":
                #     print(f"[CLIENT GAME DATA] {event.data!r}")
                elif event.kind=="rejected":
                    self.status=f"Connection rejected: {event.message}"
                    self.client.close(); self.client=None
                elif event.kind in ("disconnected","network_error"):
                    self.status=event.message or "Disconnected from host"
                    self.client.close(); self.client=None
                    self.scene="menu"; self._layout()

    def cleanup_network(self):
        if self.host: self.host.close(); self.host=None
        if self.client: self.client.close(); self.client=None

    def handle_event(self,event):
        if self.scene == "game" and self.gameplay_scene:
            self.gameplay_scene.handle_event(event)
        if event.type==pygame.QUIT: self.running=False
        elif event.type==pygame.VIDEORESIZE:
            self.screen=pygame.display.set_mode(event.size,pygame.RESIZABLE); self._layout()
        elif event.type==pygame.KEYDOWN and self.scene=="join" and self.input_active:
            if event.key==pygame.K_BACKSPACE: self.input_text=self.input_text[:-1]
            elif event.key in (pygame.K_RETURN,pygame.K_KP_ENTER): self.direct_connect()
            elif event.unicode and event.unicode.isprintable() and len(self.input_text)<64: self.input_text+=event.unicode
        elif event.type==pygame.MOUSEBUTTONDOWN and event.button==1 and self.scene=="join":
            w,h=self.size()
            self.input_active=pygame.Rect(w//2-300,int(h*.61),600,58).collidepoint(event.pos)
            for i,(ip,port,_,_) in enumerate(self.lan_hosts[:5]):
                if pygame.Rect(w//2-310,int(h*.44)+i*28,620,27).collidepoint(event.pos):
                    self.connect_to(ip,port); return
        for button in self.buttons: button.handle(event)

    def run(self):
        while self.running:
            for event in pygame.event.get(): self.handle_event(event)
            self.process_network()
            dt = self.clock.tick(60) / 1000.0
            if self.scene == "game" and self.gameplay_scene:
                self.gameplay_scene.update(dt)
            self.draw()
        self.cleanup_network()
        pygame.quit()


if __name__ == "__main__":
    DeadReplicaApp().run()
