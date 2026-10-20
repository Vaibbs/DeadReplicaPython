from __future__ import annotations

import math
import secrets
import random
from pathlib import Path

import pygame
from network_api import NetworkAPI, NetworkMessage

ASSETS = Path(__file__).resolve().parent / "assets"
PLAYER_FILE = ASSETS / "player.png"
WEAPON_FILE = ASSETS / "weapon.png"
GRASS_FILE = ASSETS / "grass_tile.png"

BASE_WORLD_WIDTH, BASE_WORLD_HEIGHT = 2400, 1800
WORLD_WIDTH_PER_PLAYER, WORLD_HEIGHT_PER_PLAYER = 800, 600
PLAYER_SPEED = 260.0
PLAYER_RADIUS = 20.0
ATTACK_RANGE = 155.0 * 0.40
SWING_ANGLE = 30.0
SWING_DURATION = 0.32
NETWORK_RATE = 1.0 / 20.0
MIN_ZOOM, MAX_ZOOM, ZOOM_STEP = 0.50, 1.45, 1.10
MAX_HEALTH = 100
ZOMBIE_SPEED = PLAYER_SPEED * 0.72
ZOMBIE_RADIUS = PLAYER_RADIUS
ZOMBIE_SPAWN_RADIUS = 520.0
ZOMBIE_MIN_SPAWN_DISTANCE = 360.0
PREP_TIME = 30.0
WAVE_BASE_ZOMBIES = 6
PLAYER_DAMAGE = 25
ZOMBIE_DAMAGE = 12
ZOMBIE_ATTACK_COOLDOWN = 1.05
DAMAGE_POPUP_TIME = 0.9
TEXT = (240, 241, 226)
HEALTH_BG = (75, 49, 47)
HEALTH_FG = (99, 205, 112)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def angle_delta(a, b):
    return (a - b + math.pi) % math.tau - math.pi


def random_player_color():
    """Use OS-backed randomness; no shared random.seed() state between clients."""
    # Random vivid color, generated once for this player and then authoritative on the wire.
    hue = secrets.randbelow(360)
    color = pygame.Color(0)
    color.hsva = (hue, 82, 98, 100)
    return [color.r, color.g, color.b]


def parse_color(value):
    if not isinstance(value, list) or len(value) != 3:
        return None
    if not all(type(v) is int and 0 <= v <= 255 for v in value):
        return None
    return list(value)


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Player:
    def __init__(self, player_id, x=0, y=0, angle=0, color=None, health=100, local=False):
        self.id = str(player_id)
        self.x, self.y, self.angle = float(x), float(y), float(angle)
        self.color = parse_color(color) or random_player_color()
        self.max_health = MAX_HEALTH
        self.health = int(clamp(int(health), 0, self.max_health))
        self.regeneration = 0.0
        self.attack_damage = PLAYER_DAMAGE
        self.loot_multiplier = 1.0
        self.upgrades = {"health": 0, "regen": 0, "damage": 0, "loot": 0, "speed": 0}
        self.regen_timer = 0.0
        self.local = local
        self.target_x, self.target_y, self.target_angle = self.x, self.y, self.angle
        self.target_health = self.health
        self.target_max_health = self.max_health
        self.target_regeneration = self.regeneration
        self.target_attack_damage = self.attack_damage
        self.target_loot_multiplier = self.loot_multiplier
        self.swing_seq = 0
        self.swing_started = None
        self.swing_offset = 0.0
        self.alive = self.health > 0
        self.invulnerable_until = 0.0
        self.speed = PLAYER_SPEED

    def packet(self):
        return {
            "id": self.id, "x": round(self.x, 3), "y": round(self.y, 3),
            "angle": round(self.angle, 5), "color": list(self.color),
            "health": int(self.health), "max_health": int(self.max_health),
            "regeneration": round(self.regeneration, 3), "attack_damage": int(self.attack_damage),
            "loot_multiplier": round(self.loot_multiplier, 3), "upgrades": dict(self.upgrades),
            "alive": bool(self.alive), "invulnerable_until": float(self.invulnerable_until), "speed": float(self.speed),
        }


class Zombie:
    def __init__(self, zombie_id, x, y, target_id, health=100, max_health=100, damage=ZOMBIE_DAMAGE, speed=ZOMBIE_SPEED, reward=1, multiplier=1):
        self.id = str(zombie_id)
        self.x, self.y = float(x), float(y)
        self.target_id = str(target_id)
        self.max_health = max(1, int(max_health))
        self.health = int(clamp(int(health), 0, self.max_health))
        self.damage = max(1, int(damage))
        self.speed = float(speed)
        self.reward = max(1, int(reward))
        self.multiplier = max(1, int(multiplier))
        self.last_hit_by = None
        self.target_x, self.target_y = self.x, self.y
        self.target_health = self.health
        self.angle = 0.0
        self.swing_started = None
        self.swing_offset = 0.0
        self.attack_timer = 0.0
        self.wander_angle = secrets.randbelow(6284) / 1000.0
        self.wander_timer = 0.0

    def packet(self):
        return {"id": self.id, "x": round(self.x, 3), "y": round(self.y, 3),
                "target_id": self.target_id, "health": int(self.health), "max_health": int(self.max_health),
                "damage": int(self.damage), "speed": round(self.speed, 3), "reward": int(self.reward),
                "multiplier": int(self.multiplier), "angle": round(self.angle, 5)}


class World:
    def __init__(self):
        self.player_count = 1
        self.width, self.height = BASE_WORLD_WIDTH, BASE_WORLD_HEIGHT
        self.texture = None
        self.scaled = None
        self.scaled_size = None
        if GRASS_FILE.exists():
            try:
                self.texture = pygame.image.load(str(GRASS_FILE)).convert()
            except pygame.error:
                pass

    def set_player_count(self, count):
        count = max(1, int(count))
        if count == self.player_count:
            return
        self.player_count = count
        self.width = BASE_WORLD_WIDTH + (count - 1) * WORLD_WIDTH_PER_PLAYER
        self.height = BASE_WORLD_HEIGHT + (count - 1) * WORLD_HEIGHT_PER_PLAYER

    def clamp_position(self, x, y, radius=PLAYER_RADIUS):
        return (
            clamp(x, -self.width / 2 + radius, self.width / 2 - radius),
            clamp(y, -self.height / 2 + radius, self.height / 2 - radius),
        )

    def draw(self, surface, camera_x, camera_y, zoom):
        sw, sh = surface.get_size()
        surface.fill((38, 55, 40))
        if self.texture:
            tw = max(1, round(self.texture.get_width() * zoom))
            th = max(1, round(self.texture.get_height() * zoom))
            if self.scaled_size != (tw, th):
                self.scaled = self.texture if self.texture.get_size() == (tw, th) else pygame.transform.smoothscale(self.texture, (tw, th))
                self.scaled_size = (tw, th)
            ox, oy = sw / 2 - camera_x * zoom, sh / 2 - camera_y * zoom
            for tx in range(math.floor(-ox / tw) - 1, math.ceil((sw - ox) / tw) + 2):
                for ty in range(math.floor(-oy / th) - 1, math.ceil((sh - oy) / th) + 2):
                    cx, cy = ox + tx * tw, oy + ty * th
                    surface.blit(self.scaled, (round(cx - tw / 2), round(cy - th / 2)))
        else:
            tile = max(8, round(64 * zoom))
            ox, oy = (sw / 2 - camera_x * zoom) % tile, (sh / 2 - camera_y * zoom) % tile
            for x in range(round(ox) - tile, sw + tile, tile):
                for y in range(round(oy) - tile, sh + tile, tile):
                    pygame.draw.rect(surface, (48, 68, 49), (x, y, tile, tile), 1)
        left = sw / 2 + (-self.width / 2 - camera_x) * zoom
        right = sw / 2 + (self.width / 2 - camera_x) * zoom
        top = sh / 2 + (-self.height / 2 - camera_y) * zoom
        bottom = sh / 2 + (self.height / 2 - camera_y) * zoom
        color = (154, 181, 137)
        if 0 <= left <= sw:
            pygame.draw.line(surface, color, (round(left), max(0, round(top))), (round(left), min(sh, round(bottom))), 2)
        if 0 <= right <= sw:
            pygame.draw.line(surface, color, (round(right), max(0, round(top))), (round(right), min(sh, round(bottom))), 2)
        if 0 <= top <= sh:
            pygame.draw.line(surface, color, (max(0, round(left)), round(top)), (min(sw, round(right)), round(top)), 2)
        if 0 <= bottom <= sh:
            pygame.draw.line(surface, color, (max(0, round(left)), round(bottom)), (min(sw, round(right)), round(bottom)), 2)


class GameplayScene:
    def __init__(self, api, on_exit=None):
        self.raw_api = api
        self.net = api if isinstance(api, NetworkAPI) else NetworkAPI(api)
        self.on_exit = on_exit
        self._register_messages()

        self.world = World()
        self.zoom = 1.0
        self.camera_x = self.camera_y = 0.0
        self.font = pygame.font.SysFont("arial", 15)
        self.small_font = pygame.font.SysFont("arial", 12)
        self.player_image = None
        self.weapon_image = None
        self.weapon_tint_cache = {}
        if WEAPON_FILE.exists():
            try:
                self.weapon_image = pygame.image.load(str(WEAPON_FILE)).convert_alpha()
                if self.weapon_image.get_size() != (64, 64):
                    self.weapon_image = pygame.transform.smoothscale(self.weapon_image, (64, 64))
            except pygame.error:
                self.weapon_image = None
        if PLAYER_FILE.exists():
            try:
                self.player_image = pygame.image.load(str(PLAYER_FILE)).convert_alpha()
            except pygame.error:
                pass
        self.tint_cache = {}
        self.local_id = self._local_id()
        self.local_player = Player(self.local_id, color=random_player_color(), local=True)
        self.players = {self.local_id: self.local_player}
        self.net_timer = 0.0
        self.mouse_held = False
        self.last_sent = None
        self.last_swing_sent = 0
        self.zombies = {}
        self.damage_popups = []
        self.wave_phase = "prep"
        self.wave_timer = PREP_TIME
        self.wave_number = 0
        self.zombie_id_counter = 0
        self.zombie_sync_timer = 0.0
        self._last_wave_packet = None
        self.shared_gold = 0
        self._pending_zombies = 0
        self._spawn_timer = 0.0
        self._upgrade_buttons = []
        self._skip_button = None
        self._revive_button_held = False
        self._revive_target_id = None
        self._revive_progress = {}
        self._remote_revive_state = {}
        self._revive_sent_timer = 0.0
        self._local_revive_started = None
        self._wave_timer_synced_at = pygame.time.get_ticks() / 1000.0
        self._skip_sent = False
        self._remote_remaining = 0
        self._send_join()

    def _register_messages(self):
        schemas = {
            "player_join": {
                "id": str, "x": is_number, "y": is_number, "angle": is_number,
                "color": NetworkAPI.color, "health": int, "max_health": int,
                "regeneration": is_number, "attack_damage": int, "loot_multiplier": is_number, "upgrades": dict,
                "alive": bool, "invulnerable_until": is_number, "speed": is_number,
            },
            "player_state": {
                "id": str, "x": is_number, "y": is_number, "angle": is_number,
                "color": NetworkAPI.color, "health": int, "max_health": int,
                "regeneration": is_number, "attack_damage": int, "loot_multiplier": is_number, "upgrades": dict,
                "alive": bool, "invulnerable_until": is_number, "speed": is_number,
            },
            "swing": {"id": str, "seq": int},
            "zombie_state": {"zombies": list},
            "wave_state": {"phase": str, "timer": is_number, "wave": int, "remaining": int},
            "damage_popup": {"id": str, "x": is_number, "y": is_number, "amount": int},
            "zombie_remove": {"id": str},
            "gold_state": {"gold": int},
            "upgrade_request": {"player_id": str, "upgrade": str},
            "revive_action": {"reviver_id": str, "target_id": str, "holding": bool},
            "skip_cooldown": {"player_id": str},
            "upgrade_state": {"player_id": str, "max_health": int, "health": int, "regeneration": is_number, "attack_damage": int, "loot_multiplier": is_number, "upgrades": dict, "gold": int, "speed": is_number},
        }
        for name, schema in schemas.items():
            self.net.register_message(name, schema, relay=(name not in ("upgrade_request", "revive_action", "skip_cooldown")))

    def _local_id(self):
        if self.net.role == "host":
            return "host"
        client = getattr(self.raw_api, "_client", None)
        peer_id = getattr(client, "peer_id", None)
        # The host overwrites the ID with the authenticated transport peer ID.
        # A random client ID prevents two clients named "client" colliding.
        return str(peer_id) if peer_id else f"client-{secrets.token_hex(6)}"

    def _send_join(self):
        p = self.local_player
        self.net.send("player_join", **p.packet())

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1:
                if getattr(self, "_death_continue_rect", None) and self._death_continue_rect.collidepoint(event.pos):
                    if self.on_exit and (len(self.players) == 1 or not any(p.id != self.local_id and p.alive for p in self.players.values())):
                        self.on_exit()
                    return
                if self._skip_button and self._skip_button.collidepoint(event.pos) and self.wave_phase == "prep":
                    if self.net.role == "host":
                        self.wave_timer = 0.0
                    else:
                        self.net.send("skip_cooldown", player_id=self.local_id)
                    return
                if self._try_upgrade_click(event.pos):
                    return
                self.mouse_held = True
                self._start_swing()
            elif event.button == 3:
                self._revive_button_held = True
                self._revive_target_id = self._find_dead_player_at(event.pos)
                self._send_revive_action()
            elif event.button == 4:
                self.zoom = clamp(self.zoom * ZOOM_STEP, MIN_ZOOM, MAX_ZOOM)
            elif event.button == 5:
                self.zoom = clamp(self.zoom / ZOOM_STEP, MIN_ZOOM, MAX_ZOOM)
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self.mouse_held = False
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 3:
            self._revive_button_held = False
            self._send_revive_action()
        elif event.type == pygame.MOUSEWHEEL:
            self.zoom = clamp(self.zoom * (ZOOM_STEP ** event.y), MIN_ZOOM, MAX_ZOOM)

    def update(self, dt):
        dt = min(max(dt, 0.0), 0.05)
        self._read_network()
        if self.net.role != "host" and self.wave_phase == "prep":
            self.wave_timer = max(0.0, self.wave_timer - dt)
        self._update_aim()
        self._move_local(dt)
        self._update_camera()
        self._update_revive(dt)
        self._update_remote_players(dt)
        self._update_swing()
        self._update_damage_popups(dt)
        # Regeneration is calculated by each player's own game instance.
        # Their health is then included in the regular player_state packet.
        self._update_player_regeneration(dt)
        if self.net.role == "host":
            self._update_host_wave(dt)
            self._update_zombies(dt)
            self._update_host_combat()
            self._update_revive_host(dt)
            self.zombie_sync_timer += dt
            if self.zombie_sync_timer >= 0.10:
                self.zombie_sync_timer = 0.0
                self._broadcast_zombies()
        else:
            self._smooth_zombies(dt)
        self.net_timer += dt
        if self.net_timer >= NETWORK_RATE:
            self.net_timer = 0.0
            self._send_state_if_changed()
        self.world.set_player_count(len(self.players))

    def _update_camera(self):
        """Keep the camera centered on the local player."""
        self.camera_x = self.local_player.x
        self.camera_y = self.local_player.y

    def _update_remote_players(self, dt):
        """Smooth remote player state between network updates."""
        factor = min(1.0, 14.0 * max(0.0, dt))
        for player in self.players.values():
            if player.local:
                continue
            player.x += (player.target_x - player.x) * factor
            player.y += (player.target_y - player.y) * factor
            diff = angle_delta(player.target_angle, player.angle)
            player.angle += diff * factor
            player.health += (player.target_health - player.health) * factor
            player.max_health += (player.target_max_health - player.max_health) * factor

    def _update_aim(self):
        mouse_x, mouse_y = pygame.mouse.get_pos()
        surface = pygame.display.get_surface()
        if surface is None:
            return
        sw, sh = surface.get_size()
        px, py = sw / 2, sh / 2
        dx, dy = mouse_x - px, mouse_y - py
        if dx or dy:
            self.local_player.angle = math.atan2(dy, dx)

    def _move_local(self, dt):
        keys = pygame.key.get_pressed()
        dx = float(keys[pygame.K_d] or keys[pygame.K_RIGHT]) - float(keys[pygame.K_a] or keys[pygame.K_LEFT])
        dy = float(keys[pygame.K_s] or keys[pygame.K_DOWN]) - float(keys[pygame.K_w] or keys[pygame.K_UP])
        length = math.hypot(dx, dy)
        if length:
            dx, dy = dx / length, dy / length
        p = self.local_player
        if not p.alive:
            return
        p.x += dx * p.speed * dt
        p.y += dy * p.speed * dt
        p.x, p.y = self.world.clamp_position(p.x, p.y)

    def _send_state_if_changed(self):
        p = self.local_player
        current = p.packet()
        previous = self.last_sent
        # Full absolute state, only transmitted if one field changed materially.
        changed = previous is None
        if previous is not None:
            changed = (
                abs(current["x"] - previous["x"]) >= 0.5
                or abs(current["y"] - previous["y"]) >= 0.5
                or abs(angle_delta(current["angle"], previous["angle"])) >= 0.01
                or current["color"] != previous["color"]
                or current["health"] != previous["health"]
                or current["alive"] != previous["alive"]
                or current["speed"] != previous["speed"]
            )
        if changed:
            self.net.send("player_state", **current)
            self.last_sent = dict(current)

    def _start_swing(self):
        p = self.local_player
        if not p.alive:
            return
        p.swing_seq += 1
        p.swing_started = pygame.time.get_ticks() / 1000.0
        p.swing_offset = 0.0
        self.net.send("swing", id=p.id, seq=p.swing_seq)

    def _update_swing(self):
        if self.mouse_held and self.local_player.alive and self.local_player.swing_started is None:
            self._start_swing()
        now = pygame.time.get_ticks() / 1000.0
        for p in self.players.values():
            if p.swing_started is None:
                p.swing_offset = 0.0
                continue
            progress = (now - p.swing_started) / SWING_DURATION
            if progress >= 1:
                p.swing_started = None
                p.swing_offset = 0.0
            elif progress < 1 / 3:
                p.swing_offset = SWING_ANGLE * progress * 3
            elif progress < 2 / 3:
                p.swing_offset = SWING_ANGLE * (1 - 2 * (progress - 1 / 3) * 3)
            else:
                p.swing_offset = -SWING_ANGLE * (1 - (progress - 2 / 3) * 3)

    def _read_network(self):
        while True:
            msg = self.net.receive(timeout=0)
            if msg is None:
                break
            self._handle_message(msg)

    def _handle_message(self, message: NetworkMessage):
        data = message.data
        peer_id = message.sender_id
        if message.name == "player_join":
            player_id = str(data["id"])
            if player_id == self.local_id:
                return
            player = self.players.get(player_id)
            if player is None:
                player = Player(player_id, data["x"], data["y"], data["angle"], data["color"], data["health"])
                self.players[player_id] = player
            self._apply_state(player, data)

            # On host, send the existing roster directly to the new peer.
            if self.net.role == "host" and peer_id is not None:
                for existing in self.players.values():
                    if existing.id != player_id:
                        self.net.send_to(peer_id, "player_join", **existing.packet())
                self.net.send_to(peer_id, "gold_state", gold=int(self.shared_gold))
                self.net.send_to(peer_id, "wave_state", phase=self.wave_phase, timer=float(self.wave_timer), wave=int(self.wave_number), remaining=int(len(self.zombies) + self._pending_zombies))

        elif message.name == "player_state":
            player_id = str(data["id"])
            player = self.players.get(player_id)
            if player is None:
                # Join establishes identity/color. Ignore state until join arrives.
                return
            if player_id == self.local_id:
                # The host is authoritative for local health/death and upgrades;
                # keep client-side movement responsive while applying those fields.
                if self.net.role != "host":
                    self.local_player.health = int(clamp(int(data["health"]), 0, max(1, int(data["max_health"]))))
                    self.local_player.max_health = max(1, int(data["max_health"]))
                    self.local_player.alive = bool(data["alive"]) and self.local_player.health > 0
                    self.local_player.invulnerable_until = float(data["invulnerable_until"])
                    self.local_player.regeneration = float(data["regeneration"])
                    self.local_player.attack_damage = int(data["attack_damage"])
                    self.local_player.loot_multiplier = float(data["loot_multiplier"])
                    self.local_player.speed = float(data["speed"])
                    self.local_player.upgrades.update({k:v for k,v in data["upgrades"].items() if k in self.local_player.upgrades and type(v) is int and v >= 0})
                return
            self._apply_state(player, data, target=True)

        elif message.name == "swing":
            player_id = str(data["id"])
            player = self.players.get(player_id)
            if player is not None and data["seq"] > player.swing_seq:
                player.swing_seq = data["seq"]
                player.swing_started = pygame.time.get_ticks() / 1000.0
                # Host resolves the hit during the active swing window in _update_host_combat.

        elif message.name == "zombie_state":
            if self.net.role != "host":
                self._apply_zombie_snapshot(data["zombies"])

        elif message.name == "wave_state":
            self.wave_phase = data["phase"]
            self.wave_timer = max(0.0, float(data["timer"]))
            self._wave_timer_synced_at = pygame.time.get_ticks() / 1000.0
            self.wave_number = max(0, int(data["wave"]))
            self._remote_remaining = max(0, int(data["remaining"]))

        elif message.name == "damage_popup":
            self._add_damage_popup(str(data["id"]), float(data["x"]), float(data["y"]), int(data["amount"]))

        elif message.name == "zombie_remove":
            self.zombies.pop(str(data["id"]), None)

        elif message.name == "gold_state":
            self.shared_gold = max(0, int(data["gold"]))

        elif message.name == "revive_action":
            if self.net.role == "host":
                reviver_id = str(data["reviver_id"])
                if peer_id is not None and reviver_id != str(peer_id):
                    return
                key = (reviver_id, str(data["target_id"]))
                if data["holding"]:
                    # A hold begins once; duplicate packets must not reset its timer.
                    self._remote_revive_state.setdefault(key, pygame.time.get_ticks() / 1000.0)
                else:
                    self._remote_revive_state.pop(key, None)

        elif message.name == "skip_cooldown":
            if self.net.role == "host" and self.wave_phase == "prep":
                player_id = str(data["player_id"])
                if peer_id is None or player_id == str(peer_id):
                    self.wave_timer = 0.0

        elif message.name == "upgrade_request":
            if self.net.role == "host":
                requested_id = str(data["player_id"])
                # A client may only purchase upgrades for its authenticated sender ID.
                if peer_id is not None and requested_id != str(peer_id):
                    return
                player = self.players.get(requested_id)
                if player is not None:
                    self._purchase_upgrade(player, str(data["upgrade"]))

        elif message.name == "upgrade_state":
            player = self.players.get(str(data["player_id"]))
            if player is not None:
                self._apply_upgrade_state(player, data)
                self.shared_gold = max(0, int(data["gold"]))

        self.world.set_player_count(len(self.players))

    def _apply_state(self, player, data, target=False):
        x, y = self.world.clamp_position(float(data["x"]), float(data["y"]))
        angle = float(data["angle"])
        color = parse_color(data["color"])
        max_health = max(1, int(data.get("max_health", MAX_HEALTH)))
        health = int(clamp(int(data["health"]), 0, max_health))
        if target:
            player.target_x, player.target_y, player.target_angle = x, y, angle
            player.target_health = health
            player.target_max_health = max_health
            # Alive/dead is authoritative and must not wait for smoothed HP.
            player.alive = bool(data.get("alive", health > 0)) and health > 0
            player.invulnerable_until = float(data.get("invulnerable_until", player.invulnerable_until))
        else:
            player.x, player.y, player.angle = x, y, angle
            player.target_x, player.target_y, player.target_angle = x, y, angle
            player.health = health
            player.alive = bool(data.get("alive", health > 0)) and health > 0
            player.invulnerable_until = float(data.get("invulnerable_until", player.invulnerable_until))
            player.speed = max(1.0, float(data.get("speed", player.speed)))
            player.target_health = health
            player.max_health = max_health
        if color:
            player.color = color
        player.regeneration = max(0.0, float(data.get("regeneration", player.regeneration)))
        player.attack_damage = max(1, int(data.get("attack_damage", player.attack_damage)))
        player.alive = bool(data.get("alive", player.health > 0)) and player.health > 0
        player.invulnerable_until = float(data.get("invulnerable_until", player.invulnerable_until))
        player.speed = max(1.0, float(data.get("speed", player.speed)))
        player.loot_multiplier = max(0.01, float(data.get("loot_multiplier", player.loot_multiplier)))
        upgrades = data.get("upgrades")
        if isinstance(upgrades, dict):
            for k in player.upgrades:
                if type(upgrades.get(k)) is int and upgrades[k] >= 0:
                    player.upgrades[k] = upgrades[k]

    def _tinted_sprite(self, color):
        if self.player_image is None:
            return None
        key = tuple(color)
        if key not in self.tint_cache:
            image = self.player_image.copy()
            tint = pygame.Surface(image.get_size(), pygame.SRCALPHA)
            tint.fill((*color, 255))
            # RGB gets multiplied; alpha remains from the source texture.
            image.blit(tint, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            self.tint_cache[key] = image
        return self.tint_cache[key]

    def _damage_color(self, t):
        omega, k = 2.399963229728653, 0.35
        hue = ((omega * t) % math.tau) / math.tau
        saturation = 1.0 - math.exp(-k * t)
        c = pygame.Color(0)
        c.hsva = (hue * 360.0, saturation * 100.0, 100.0, 100.0)
        return (c.r, c.g, c.b)

    def _weapon_tinted(self, t):
        if self.weapon_image is None:
            return None
        color = self._damage_color(t)
        key = (int(t), color)
        if key not in self.weapon_tint_cache:
            image = self.weapon_image.copy()
            tint = pygame.Surface(image.get_size(), pygame.SRCALPHA)
            tint.fill((*color, 255))
            image.blit(tint, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            self.weapon_tint_cache[key] = image
        return self.weapon_tint_cache[key]

    def _world_to_screen(self, x, y, size):
        sw, sh = size
        return round(sw / 2 + (x - self.camera_x) * self.zoom), round(sh / 2 + (y - self.camera_y) * self.zoom)

    def _draw_player(self, surface, p):
        center = self._world_to_screen(p.x, p.y, surface.get_size())
        radius = max(2, round(PLAYER_RADIUS * self.zoom))
        aim = p.angle + math.radians(p.swing_offset)
        sprite = self._tinted_sprite(p.color)
        if sprite is not None:
            size = max(1, round(PLAYER_RADIUS * 2.8 * self.zoom))
            scaled = pygame.transform.smoothscale(sprite, (size, size))
            rotated = pygame.transform.rotate(scaled, -math.degrees(aim))
            surface.blit(rotated, rotated.get_rect(center=center))
        else:
            pygame.draw.circle(surface, p.color, center, radius)
            pygame.draw.line(surface, (25, 30, 25), center, (round(center[0] + math.cos(aim) * radius), round(center[1] + math.sin(aim) * radius)), 3)

        # Weapon is visible once at least one damage upgrade is purchased.
        if p.upgrades.get("damage", 0) > 0:
            weapon = self._weapon_tinted(p.upgrades["damage"])
            if weapon is not None:
                ws = max(1, round(64 * self.zoom))
                weapon = pygame.transform.smoothscale(weapon, (ws, ws))
                weapon = pygame.transform.rotate(weapon, -math.degrees(aim))
                offset = max(radius, round(PLAYER_RADIUS * 1.05 * self.zoom))
                wc = (round(center[0] + math.cos(aim) * offset), round(center[1] + math.sin(aim) * offset))
                surface.blit(weapon, weapon.get_rect(center=wc))
            else:
                wc = (round(center[0] + math.cos(aim) * radius), round(center[1] + math.sin(aim) * radius))
                pygame.draw.line(surface, self._damage_color(p.upgrades["damage"]), center, wc, max(3, round(6*self.zoom)))

        bw, bh = max(18, round(34 * self.zoom)), max(3, round(4 * self.zoom))
        bx, by = center[0] - bw // 2, center[1] + radius + 5
        pygame.draw.rect(surface, HEALTH_BG, (bx, by, bw, bh), border_radius=2)
        fill = round(bw * clamp(p.health / max(1, p.max_health), 0, 1))
        if fill:
            pygame.draw.rect(surface, HEALTH_FG, (bx, by, fill, bh), border_radius=2)

    def _draw_team_overlay(self, surface):
        # No sidebar: just a small transparent name/health overlay.
        rows = [self.local_player] + [p for p in self.players.values() if not p.local]
        height = min(surface.get_height(), 18 + 29 * len(rows))
        panel = pygame.Surface((210, height), pygame.SRCALPHA)
        panel.fill((18, 25, 20, 145))
        surface.blit(panel, (10, 10))
        y = 17
        for p in rows:
            pygame.draw.circle(surface, p.color, (22, y + 6), 5)
            name = "You" if p.local else p.id[:14]
            surface.blit(self.small_font.render(name, True, TEXT), (33, y))
            bx, by, bw, bh = 112, y + 2, 92, 7
            pygame.draw.rect(surface, HEALTH_BG, (bx, by, bw, bh), border_radius=3)
            fill = round(bw * clamp(p.health / max(1, p.max_health), 0, 1))
            if fill:
                pygame.draw.rect(surface, HEALTH_FG, (bx, by, fill, bh), border_radius=3)
            y += 29

    def draw(self, surface):
        self.world.draw(surface, self.camera_x, self.camera_y, self.zoom)
        for z in self.zombies.values():
            self._draw_zombie(surface, z)
        for p in self.players.values():
            self._draw_player(surface, p)
        self._draw_team_overlay(surface)
        self._draw_wave_bar(surface)
        self._draw_gold_and_upgrades(surface)
        self._draw_death_and_revive(surface)


    def _update_host_wave(self, dt):
        self.wave_timer -= dt
        if self.wave_phase == "prep":
            if self.wave_timer <= 0:
                self.wave_phase = "wave"
                self.wave_number += 1
                count = WAVE_BASE_ZOMBIES + max(0, len(self.players) - 1) * 2 + (self.wave_number - 1) * 2
                self._pending_zombies = count
                self._spawn_queued_if_possible()
                self.wave_timer = 0.0
        elif self.wave_phase == "wave" and not self.zombies and self._pending_zombies <= 0:
            self.wave_phase = "prep"
            self.wave_timer = PREP_TIME
        self._broadcast_wave_state()

    def _spawn_queued_if_possible(self):
        # Keep at most four live zombies per player. Surplus are spawned as slots open.
        cap = max(1, len(self.players)) * 4
        while self._pending_zombies > 0 and len(self.zombies) < cap:
            before = len(self.zombies)
            self._spawn_zombie()
            if len(self.zombies) == before:
                break
            self._pending_zombies -= 1

    def _choose_zombie_target(self, x, y):
        players = [p for p in self.players.values() if p.alive and p.health > 0]
        if not players:
            return None
        if not players:
            return None
        loads = {p.id: 0 for p in players}
        for z in self.zombies.values():
            if z.target_id in loads:
                loads[z.target_id] += 1
        eligible = [p for p in players if loads[p.id] < 6]
        if not eligible:
            eligible = sorted(players, key=lambda p: loads[p.id])[:1]
        return min(eligible, key=lambda p: math.hypot(p.x-x, p.y-y))

    def _spawn_zombie(self):
        if not self.players:
            self._pending_zombies = 0
            return
        # Spawn around a player, outside the minimum radius, inside the world.
        anchor = list(self.players.values())[secrets.randbelow(len(self.players))]
        angle = secrets.randbelow(6284) / 1000.0
        distance = ZOMBIE_MIN_SPAWN_DISTANCE + secrets.randbelow(max(1, int(ZOMBIE_SPAWN_RADIUS - ZOMBIE_MIN_SPAWN_DISTANCE)))
        x, y = self.world.clamp_position(anchor.x + math.cos(angle)*distance, anchor.y + math.sin(angle)*distance, ZOMBIE_RADIUS)
        target = self._choose_zombie_target(x, y)
        if target is None:
            return
        # Weighted multiplier: 1=50%, 2=25%, 3=12.5%, etc.
        roll = secrets.randbelow(2**20)
        threshold = 2**19
        multiplier = 1
        while roll >= threshold and multiplier < 20:
            multiplier += 1
            threshold += 2**(20-multiplier)
        scale = self.wave_number * 0.2 * multiplier
        max_health = max(1, round(MAX_HEALTH * (1.0 + scale)))
        damage = max(1, round(ZOMBIE_DAMAGE * (1.0 + scale)))
        speed = ZOMBIE_SPEED * (1.0 + min(0.75, scale * 0.12))
        reward = max(1, round((max_health / 10.0 + damage) * (1.0 + scale * 0.25)))
        self.zombie_id_counter += 1
        zid = f"z{self.wave_number}-{self.zombie_id_counter}"
        self.zombies[zid] = Zombie(zid, x, y, target.id, max_health, max_health, damage, speed, reward, multiplier)

    def _update_zombies(self, dt):
        for z in list(self.zombies.values()):
            target = self.players.get(z.target_id)
            if target is None or not target.alive:
                target = self._choose_zombie_target(z.x, z.y)
                if target is not None:
                    z.target_id = target.id
            if target is None:
                z.wander_timer -= dt
                if z.wander_timer <= 0:
                    z.wander_angle = secrets.randbelow(6284) / 1000.0
                    z.wander_timer = 1.0 + secrets.randbelow(300) / 100.0
                z.x += math.cos(z.wander_angle) * z.speed * 0.25 * dt
                z.y += math.sin(z.wander_angle) * z.speed * 0.25 * dt
                z.x, z.y = self.world.clamp_position(z.x, z.y, ZOMBIE_RADIUS)
                z.angle = z.wander_angle
                continue
            dx, dy = target.x - z.x, target.y - z.y
            distance = math.hypot(dx, dy)
            if distance > 0.001:
                z.angle = math.atan2(dy, dx)
            if distance > PLAYER_RADIUS + ZOMBIE_RADIUS + 2:
                z.x += dx / max(distance, 0.001) * z.speed * dt
                z.y += dy / max(distance, 0.001) * z.speed * dt
                z.x, z.y = self.world.clamp_position(z.x, z.y, ZOMBIE_RADIUS)
            z.attack_timer = max(0.0, z.attack_timer - dt)
            if target.alive and distance <= PLAYER_RADIUS + ZOMBIE_RADIUS + 8 and z.attack_timer <= 0:
                z.swing_started = pygame.time.get_ticks() / 1000.0
                z.attack_timer = ZOMBIE_ATTACK_COOLDOWN
                self._damage_player(target, z.damage)

    def _update_host_combat(self):
        # Player attacks are resolved only against zombies; friendly fire is disabled.
        # Host local swings are handled in _start_swing; this checks only active remote swings
        # once per animation, using swing_seq as the idempotency key.
        if not hasattr(self, "_resolved_swings"):
            self._resolved_swings = {}
        now = pygame.time.get_ticks() / 1000.0
        for p in self.players.values():
            if not p.alive or p.swing_started is None or now - p.swing_started < SWING_DURATION * 0.35:
                continue
            seq = p.swing_seq
            key = (p.id, seq)
            if key in self._resolved_swings:
                continue
            self._resolved_swings[key] = now
            self._resolve_player_attack(p)
        self._resolved_swings = {k:v for k,v in self._resolved_swings.items() if now-v < 5.0}

    def _resolve_player_attack(self, player):
        if not player.alive:
            return
        # One damage application per swing sequence.
        if not hasattr(self, "_damage_seen_swings"):
            self._damage_seen_swings = set()
        key = (player.id, player.swing_seq)
        if key in self._damage_seen_swings:
            return
        self._damage_seen_swings.add(key)
        facing = player.angle
        for z in list(self.zombies.values()):
            dx, dy = z.x-player.x, z.y-player.y
            distance = math.hypot(dx, dy)
            if distance > ATTACK_RANGE + PLAYER_RADIUS + ZOMBIE_RADIUS:
                continue
            if distance and abs(angle_delta(math.atan2(dy, dx), facing)) > math.radians(SWING_ANGLE):
                continue
            damage = int(player.attack_damage)
            z.health -= damage
            z.last_hit_by = player.id
            self._add_damage_popup(z.id, z.x, z.y, damage)
            self.net.send("damage_popup", id=z.id, x=z.x, y=z.y, amount=damage)
            if z.health <= 0:
                killer = self.players.get(z.last_hit_by)
                if killer is not None:
                    earned = max(1, math.ceil(z.reward * killer.loot_multiplier))
                    self.shared_gold += earned
                    self.net.send("gold_state", gold=self.shared_gold)
                self.zombies.pop(z.id, None)
                self.net.send("zombie_remove", id=z.id)
                self._spawn_queued_if_possible()
        if len(self._damage_seen_swings) > 5000:
            self._damage_seen_swings.clear()

    def _damage_player(self, player, amount):
        if player.health <= 0 or pygame.time.get_ticks()/1000.0 < player.invulnerable_until:
            return
        player.health = max(0, player.health - amount)
        if player.health <= 0:
            player.health = 0
            player.alive = False
            player.swing_started = None
        player.target_health = player.health
        self._add_damage_popup(player.id, player.x, player.y, amount)
        self.net.send("damage_popup", id=player.id, x=player.x, y=player.y, amount=amount)
        # Host publishes authoritative health for local and remote players.
        if self.net.role == "host":
            self.net.send("player_state", **player.packet())
        elif player.local:
            self._send_state_if_changed()

    def _broadcast_zombies(self):
        payload = [z.packet() for z in self.zombies.values()]
        self.net.send("zombie_state", zombies=payload)

    def _apply_zombie_snapshot(self, rows):
        if not isinstance(rows, list):
            return
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                zid = str(row["id"]); x = float(row["x"]); y = float(row["y"])
                target_id = str(row["target_id"]); max_health = max(1, int(row.get("max_health", MAX_HEALTH)))
                health = int(clamp(int(row["health"]), 0, max_health))
                angle = float(row.get("angle", 0.0))
                if not all(math.isfinite(v) for v in (x, y, angle)):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            seen.add(zid)
            z = self.zombies.get(zid)
            if z is None:
                z = Zombie(zid, x, y, target_id, health, max_health, row.get("damage", ZOMBIE_DAMAGE), row.get("speed", ZOMBIE_SPEED), row.get("reward", 1), row.get("multiplier", 1))
                self.zombies[zid] = z
            z.max_health = max_health
            z.damage = max(1, int(row.get("damage", z.damage)))
            z.speed = float(row.get("speed", z.speed))
            z.reward = max(1, int(row.get("reward", z.reward)))
            z.multiplier = max(1, int(row.get("multiplier", z.multiplier)))
            z.target_x, z.target_y, z.target_health = x, y, health
            z.target_id, z.angle = target_id, angle
        for zid in list(self.zombies):
            if zid not in seen:
                self.zombies.pop(zid, None)

    def _smooth_zombies(self, dt):
        factor = min(1.0, 12.0 * dt)
        for z in self.zombies.values():
            z.x += (z.target_x-z.x) * factor
            z.y += (z.target_y-z.y) * factor
            z.health += (z.target_health-z.health) * factor

    def _add_damage_popup(self, entity_id, x, y, amount):
        self.damage_popups.append({"id": entity_id, "x": x, "y": y, "amount": amount, "age": 0.0})

    def _update_damage_popups(self, dt):
        for popup in self.damage_popups:
            popup["age"] += dt
            popup["y"] -= 24 * dt
        self.damage_popups = [p for p in self.damage_popups if p["age"] < DAMAGE_POPUP_TIME]

    def _broadcast_wave_state(self):
        remaining = (len(self.zombies) + self._pending_zombies) if self.net.role == "host" else getattr(self, "_remote_remaining", 0)
        packet = {"phase": self.wave_phase, "timer": round(self.wave_timer, 2), "wave": self.wave_number, "remaining": remaining}
        key = (packet["phase"], int(packet["timer"]), packet["wave"], packet["remaining"])
        if key != self._last_wave_packet:
            self.net.send("wave_state", **packet)
            self._last_wave_packet = key

    def _draw_zombie(self, surface, z):
        center = self._world_to_screen(z.x, z.y, surface.get_size())
        radius = max(2, round(ZOMBIE_RADIUS * self.zoom))
        # Same player texture, consistently dark-green tinted.
        sprite = self._tinted_sprite((38, 92, 43))
        if sprite is not None:
            size = max(1, round(PLAYER_RADIUS * 2.8 * self.zoom))
            scaled = pygame.transform.smoothscale(sprite, (size, size))
            rotated = pygame.transform.rotate(scaled, -math.degrees(z.angle))
            surface.blit(rotated, rotated.get_rect(center=center))
        else:
            pygame.draw.circle(surface, (38, 92, 43), center, radius)
        if z.damage > ZOMBIE_DAMAGE:
            t = max(1, round(z.damage / ZOMBIE_DAMAGE) - 1)
            weapon = self._weapon_tinted(t)
            if weapon is not None:
                now = pygame.time.get_ticks()/1000.0
                swing = 0.0
                if z.swing_started is not None:
                    progress = clamp((now-z.swing_started)/SWING_DURATION, 0, 1)
                    swing = math.sin(progress*math.pi)*SWING_ANGLE
                weapon_angle = z.angle + math.radians(swing)
                ws = max(1, round(64*self.zoom))
                weapon = pygame.transform.rotate(pygame.transform.smoothscale(weapon,(ws,ws)), -math.degrees(weapon_angle))
                wc = (round(center[0]+math.cos(weapon_angle)*radius), round(center[1]+math.sin(weapon_angle)*radius))
                surface.blit(weapon, weapon.get_rect(center=wc))
        bw, bh = max(18, round(34*self.zoom)), max(3, round(4*self.zoom))
        bx, by = center[0]-bw//2, center[1]+radius+5
        pygame.draw.rect(surface, HEALTH_BG, (bx, by, bw, bh), border_radius=2)
        fill = round(bw * clamp(z.health/max(1, z.max_health), 0, 1))
        if fill:
            pygame.draw.rect(surface, HEALTH_FG, (bx, by, fill, bh), border_radius=2)
        for popup in self.damage_popups:
            if popup["id"] == z.id:
                pos = self._world_to_screen(popup["x"], popup["y"]-popup["age"]*24, surface.get_size())
                txt = self.font.render(f"-{popup['amount']}", True, (255, 105, 90))
                surface.blit(txt, txt.get_rect(center=(pos[0], pos[1]-24)))

    def _upgrade_cost(self, player, upgrade):
        levels = player.upgrades
        if upgrade == "health":
            return math.ceil(1 * (1.1 ** levels["health"]))
        if upgrade == "regen":
            return math.ceil(1 * (1.2 ** levels["regen"]))
        if upgrade == "damage":
            return math.ceil(1 * (1.25 ** levels["damage"]))
        if upgrade == "loot":
            return math.ceil(10 * (1.5 ** levels["loot"]))
        if upgrade == "speed":
            return math.ceil(1 * (1.35 ** levels["speed"]))
        return 10**9

    def _try_upgrade_click(self, pos):
        for rect, upgrade in self._upgrade_buttons:
            if rect.collidepoint(pos):
                if self.net.role == "host":
                    self._purchase_upgrade(self.local_player, upgrade)
                else:
                    self.net.send("upgrade_request", player_id=self.local_id, upgrade=upgrade)
                return True
        return False

    def _purchase_upgrade(self, player, upgrade):
        if upgrade not in player.upgrades:
            return
        cost = self._upgrade_cost(player, upgrade)
        if self.shared_gold < cost:
            return
        self.shared_gold -= cost
        player.upgrades[upgrade] += 1
        if upgrade == "health":
            player.max_health += 1
            player.health = min(player.max_health, player.health + 1)
        elif upgrade == "regen":
            player.regeneration += 1.0
        elif upgrade == "damage":
            player.attack_damage += 10
        elif upgrade == "loot":
            player.loot_multiplier = round(player.loot_multiplier + 0.01, 4)
        elif upgrade == "speed":
            player.speed += 1.0
        payload = {
            "player_id": player.id, "max_health": int(player.max_health), "health": int(player.health),
            "regeneration": float(player.regeneration), "attack_damage": int(player.attack_damage),
            "loot_multiplier": float(player.loot_multiplier), "upgrades": dict(player.upgrades), "gold": int(self.shared_gold), "speed": float(player.speed),
        }
        self.net.send("upgrade_state", **payload)
        self.net.send("gold_state", gold=int(self.shared_gold))
        self.net.send("player_state", **player.packet())

    def _apply_upgrade_state(self, player, data):
        player.max_health = max(1, int(data["max_health"]))
        player.health = int(clamp(int(data["health"]), 0, player.max_health))
        player.target_health = player.health
        player.regeneration = max(0.0, float(data["regeneration"]))
        player.attack_damage = max(1, int(data["attack_damage"]))
        player.loot_multiplier = max(0.01, float(data["loot_multiplier"]))
        player.speed = max(1.0, float(data.get("speed", player.speed)))
        if isinstance(data["upgrades"], dict):
            for key in player.upgrades:
                value = data["upgrades"].get(key)
                if type(value) is int and value >= 0:
                    player.upgrades[key] = value

    def _update_player_regeneration(self, dt):
        # Only regenerate this instance's local player. In multiplayer, clients
        # report their updated health through player_state; the host must not
        # independently regenerate remote players or overwrite that value.
        player = self.local_player
        if player.alive and player.health > 0 and player.regeneration > 0 and player.health < player.max_health:
            player.health = min(player.max_health, player.health + player.regeneration * dt)
            player.target_health = player.health

    def _draw_gold_and_upgrades(self, surface):
        gold_text = self.font.render(f"GOLD: {self.shared_gold}", True, (255, 218, 92))
        gold_bg = pygame.Surface((gold_text.get_width()+20, 32), pygame.SRCALPHA)
        gold_bg.fill((12, 17, 13, 190))
        gx = surface.get_width() - gold_bg.get_width() - 12
        surface.blit(gold_bg, (gx, 12))
        surface.blit(gold_text, (gx+10, 19))

        labels = [
            ("health", f"Max HP +1  ({self.local_player.max_health})"),
            ("regen", f"Regen +1/s  ({self.local_player.regeneration:g})"),
            ("damage", f"Damage +10  ({self.local_player.attack_damage})"),
            ("loot", f"Loot +0.01x  ({self.local_player.loot_multiplier:.2f}x)"),
            ("speed", f"Speed +1  ({self.local_player.speed:g})"),
        ]
        panel_w, row_h = 292, 27
        panel_h = len(labels)*row_h + 12
        x, y = surface.get_width()-panel_w-12, surface.get_height()-panel_h-12
        panel = pygame.Surface((panel_w, panel_h), pygame.SRCALPHA)
        panel.fill((12, 17, 13, 185)); surface.blit(panel, (x,y))
        self._upgrade_buttons = []
        for i, (key, label) in enumerate(labels):
            row_y = y+6+i*row_h
            cost = self._upgrade_cost(self.local_player, key)
            txt = self.small_font.render(f"{label}  {cost}g", True, TEXT)
            surface.blit(txt, (x+8, row_y+5))
            btn = pygame.Rect(x+panel_w-28, row_y+2, 22, 22)
            enabled = self.shared_gold >= cost
            pygame.draw.rect(surface, (63, 137, 73) if enabled else (70, 74, 68), btn, border_radius=4)
            plus = self.font.render("+", True, (255,255,255))
            surface.blit(plus, plus.get_rect(center=btn.center))
            self._upgrade_buttons.append((btn, key))

    def _find_dead_player_at(self, pos):
        for p in self.players.values():
            if p.id == self.local_id or p.alive:
                continue
            center = self._world_to_screen(p.x, p.y, pygame.display.get_surface().get_size())
            if math.hypot(pos[0]-center[0], pos[1]-center[1]) <= max(26, PLAYER_RADIUS*self.zoom+8):
                return p.id
        return None

    def _send_revive_action(self):
        if self._revive_button_held and self._revive_target_id:
            self.net.send("revive_action", reviver_id=self.local_id, target_id=self._revive_target_id, holding=True)
        else:
            self.net.send("revive_action", reviver_id=self.local_id, target_id=self._revive_target_id or "", holding=False)

    def _update_revive(self, dt):
        if not self._revive_button_held or not self.local_player.alive:
            self._revive_target_id = None
            self._local_revive_started = None
            return

        pos = pygame.mouse.get_pos()
        target_id = self._find_dead_player_at(pos)
        if target_id != self._revive_target_id:
            old_target = self._revive_target_id
            if old_target:
                self.net.send("revive_action", reviver_id=self.local_id, target_id=old_target, holding=False)
            self._revive_target_id = target_id
            self._local_revive_started = pygame.time.get_ticks() / 1000.0 if target_id else None
            if target_id:
                self._send_revive_action()

        target = self.players.get(self._revive_target_id) if self._revive_target_id else None
        if target is None or target.alive:
            self._local_revive_started = None
            return
        distance = math.hypot(self.local_player.x - target.x, self.local_player.y - target.y)
        if distance > PLAYER_RADIUS * 3.5:
            self._local_revive_started = None
        elif self._local_revive_started is None:
            self._local_revive_started = pygame.time.get_ticks() / 1000.0

    def _update_revive_host(self, dt):
        now = pygame.time.get_ticks() / 1000.0
        for key, started in list(self._remote_revive_state.items()):
            reviver = self.players.get(key[0])
            target = self.players.get(key[1])
            if reviver is None or target is None or not reviver.alive or target.alive:
                self._remote_revive_state.pop(key, None)
                continue
            if math.hypot(reviver.x - target.x, reviver.y - target.y) > PLAYER_RADIUS * 3.5:
                # Keep the hold active but require a fresh five seconds once back in range.
                self._remote_revive_state[key] = now
                continue
            if now - started >= 5.0:
                target.health = target.max_health
                target.target_health = target.health
                target.alive = True
                target.invulnerable_until = now + 3.0
                target.swing_started = None
                target.packet()  # Refresh the authoritative fields before broadcast.
                self.net.send("player_state", **target.packet())
                self._remote_revive_state.pop(key, None)

        # Host-local revive uses the same timestamp-based five-second rule.
        if self._revive_button_held and self._revive_target_id:
            reviver = self.local_player
            target = self.players.get(self._revive_target_id)
            key = (reviver.id, self._revive_target_id)
            if (target is not None and not target.alive and reviver.alive
                    and math.hypot(reviver.x - target.x, reviver.y - target.y) <= PLAYER_RADIUS * 3.5):
                started = self._revive_progress.setdefault(key, now)
                if now - started >= 5.0:
                    target.health = target.max_health
                    target.target_health = target.health
                    target.alive = True
                    target.invulnerable_until = now + 3.0
                    target.swing_started = None
                    self.net.send("player_state", **target.packet())
                    self._revive_progress.pop(key, None)
            else:
                self._revive_progress.pop(key, None)
        else:
            for key in list(self._revive_progress):
                if key[0] == self.local_id:
                    self._revive_progress.pop(key, None)

    def _draw_death_and_revive(self, surface):
        if not self.local_player.alive:
            shade = pygame.Surface(surface.get_size(), pygame.SRCALPHA); shade.fill((0,0,0,130)); surface.blit(shade,(0,0))
            title = pygame.font.SysFont("arial", 44, bold=True).render("YOU DIED", True, (245,80,70))
            surface.blit(title, title.get_rect(center=(surface.get_width()//2, surface.get_height()//2-24)))
            alive_others = [p for p in self.players.values() if p.id != self.local_id and p.alive]
            if len(self.players) == 1 or not alive_others:
                msg = self.font.render("Continue to lobby", True, TEXT)
                rect = pygame.Rect(0,0,msg.get_width()+30,34); rect.center=(surface.get_width()//2,surface.get_height()//2+28)
                pygame.draw.rect(surface,(65,100,68),rect,border_radius=5); surface.blit(msg,msg.get_rect(center=rect.center))
                self._death_continue_rect = rect
            else:
                msg = self.font.render("Waiting for a teammate to revive you (hold right-click for 5 seconds)", True, TEXT)
                surface.blit(msg,msg.get_rect(center=(surface.get_width()//2,surface.get_height()//2+28)))
        else:
            self._death_continue_rect = None

        # Show revive progress only to the player doing the reviving.
        if self._revive_button_held and self._revive_target_id and self.local_player.alive:
            target = self.players.get(self._revive_target_id)
            if target is not None and not target.alive:
                distance = math.hypot(self.local_player.x - target.x, self.local_player.y - target.y)
                if distance <= PLAYER_RADIUS * 3.5:
                    now = pygame.time.get_ticks() / 1000.0
                    started = self._local_revive_started if self._local_revive_started is not None else now
                    progress = clamp((now - started) / 5.0, 0.0, 1.0)
                    label = self.font.render("REVIVING...  Hold right-click", True, TEXT)
                    cx, cy = surface.get_width() // 2, surface.get_height() // 2 + 70
                    surface.blit(label, label.get_rect(center=(cx, cy - 13)))
                    bar = pygame.Rect(cx - 130, cy + 5, 260, 14)
                    pygame.draw.rect(surface, (25, 30, 26), bar, border_radius=7)
                    if progress > 0:
                        fill = pygame.Rect(bar.x, bar.y, max(1, round(bar.width * progress)), bar.height)
                        pygame.draw.rect(surface, (105, 210, 125), fill, border_radius=7)
                    pygame.draw.rect(surface, (220, 230, 215), bar, 1, border_radius=7)

        if self._skip_button:
            self._skip_button = None
        if self.wave_phase == "prep":
            rect = pygame.Rect(surface.get_width()-150, 52, 138, 28)
            pygame.draw.rect(surface, (65,100,68), rect, border_radius=4)
            surface.blit(self.small_font.render("Skip cooldown", True, TEXT), self.small_font.render("Skip cooldown", True, TEXT).get_rect(center=rect.center))
            self._skip_button = rect

    def _draw_wave_bar(self, surface):
        w = min(460, surface.get_width()-40); h = 34
        x = (surface.get_width()-w)//2; y = 12
        panel = pygame.Surface((w, h), pygame.SRCALPHA); panel.fill((12, 17, 13, 185)); surface.blit(panel, (x,y))
        if self.wave_phase == "prep":
            label = f"PREP TIME  {max(0, math.ceil(self.wave_timer))}s  •  NEXT WAVE {self.wave_number+1}"
            progress = 1.0-clamp(self.wave_timer/PREP_TIME, 0, 1)
        else:
            remaining = (len(self.zombies) + self._pending_zombies) if self.net.role == "host" else getattr(self, "_remote_remaining", 0)
            label = f"WAVE {self.wave_number}  •  ZOMBIES REMAINING: {remaining}"
            total = max(1, WAVE_BASE_ZOMBIES + max(0,len(self.players)-1)*2+(self.wave_number-1)*2)
            progress = 1.0 - clamp(remaining / total, 0, 1)
        surface.blit(self.small_font.render(label, True, TEXT), (x+10,y+3))
        bar_x, bar_y, bar_w, bar_h = x+10, y+21, w-20, 6
        pygame.draw.rect(surface, (65,70,61), (bar_x,bar_y,bar_w,bar_h), border_radius=3)
        if progress > 0:
            pygame.draw.rect(surface, (112,190,93), (bar_x,bar_y,round(bar_w*progress),bar_h), border_radius=3)
        for popup in self.damage_popups:
            # Player popups are drawn here so remote damage is visible too.
            if popup["id"] in self.players:
                pos = self._world_to_screen(popup["x"], popup["y"]-popup["age"]*24, surface.get_size())
                txt = self.font.render(f"-{popup['amount']}", True, (255,105,90))
                surface.blit(txt, txt.get_rect(center=(pos[0],pos[1]-24)))
