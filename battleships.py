#!/usr/bin/env python3
"""
BATTLESHIPS - terminal edition + LAN (Python 3.8+, no dependencies)
Run:   python battleships.py
python battleships.py --no-color     (if colors look wrong)

AI levels:
Easy       random shots
Medium     random search, then hunts around hits
Hard       checkerboard search, follows lines of hits
Expert     probability map over every possible ship position
Nightmare  Monte Carlo rollouts, picks fewest expected shots
"""
from __future__ import annotations

import argparse
import math
import os
import random
import re
import sys

import socket
import threading
import json
import time
import hashlib
import hmac
import queue
import secrets
import collections

from dataclasses import dataclass, field
from typing import (
    Any, Callable, Dict, FrozenSet, List, Optional, Protocol, Sequence,
    Set, Tuple, Union,
)

# ----------------------------------------------------------------------------
# Explicit contracts (typing) — single source of truth for data flow
# ----------------------------------------------------------------------------
Cell = Tuple[int, int]
ShipCells = Dict[str, List[Cell]]
ShotResult = Tuple[bool, Optional[str], bool]


class Shooter(Protocol):
    """Explicit contract for anything that can choose/record shots."""
    def choose(self) -> Cell: ...
    def record(self, pos: Cell, hit: bool, sunk_len: Optional[int] = None) -> None: ...


class BoardLike(Protocol):
    def fire(self, pos: Cell) -> ShotResult: ...
    def all_sunk(self) -> bool: ...


@dataclass
class BoardConfig:
    """Centralized board/fleet configuration (replaces scattered globals)."""
    size: int = 10
    cols: str = "ABCDEFGHIJ"
    fleet: List[Tuple[str, int]] = field(default_factory=lambda: [
        ("Carrier", 5), ("Battleship", 4), ("Cruiser", 3),
        ("Submarine", 3), ("Destroyer", 2),
    ])

    @property
    def ship_len(self) -> Dict[str, int]:
        return dict(self.fleet)

    @property
    def fleet_lengths(self) -> List[int]:
        return [n for _, n in self.fleet]


_CONFIG = BoardConfig()


@dataclass
class VisualSettings:
    """Cohesive visual flags (replaces bare VISUAL dict)."""
    flags: Dict[str, bool] = field(default_factory=lambda: dict(VISUAL_DEFAULTS))

    def get(self, name: str, default: bool = False) -> bool:
        return self.flags.get(name, default)

    def __getitem__(self, name: str) -> bool:
        return self.flags.get(name, False)

    def __setitem__(self, name: str, value: bool) -> None:
        self.flags[name] = bool(value)

    def toggle(self, name: str) -> bool:
        self.flags[name] = not self.flags.get(name, False)
        return self.flags[name]

    def reset(self) -> None:
        self.flags.clear()
        self.flags.update(VISUAL_DEFAULTS)


@dataclass
class MenuState:
    """Replaces function/class-level last-choice attributes."""
    last_setup_choice: int = 0
    last_level: int = 0
    last_mode: int = 0
    last_tactic: int = 0
    last_game_setup: int = 0


_MENU_STATE = MenuState()


@dataclass
class TerminalState:
    """Owns terminal capability flags (replaces bare USE_COLOR/ANSI_OK writes)."""
    use_color: bool = False
    ansi_ok: bool = False


_TERM_STATE = TerminalState()


def set_terminal_state(use_color: bool, ansi_ok: bool) -> None:
    """Single writer for terminal flags; keeps globals in sync for compat."""
    global USE_COLOR, ANSI_OK
    USE_COLOR = bool(use_color)
    ANSI_OK = bool(ansi_ok)
    _TERM_STATE.use_color = bool(use_color)
    _TERM_STATE.ansi_ok = bool(ansi_ok)


def is_color_enabled() -> bool:
    return _TERM_STATE.use_color


def is_ansi_ok() -> bool:
    return _TERM_STATE.ansi_ok


@dataclass
class GameStats:
    """Typed wrapper around the stats dict kept for compatibility."""
    shots: int = 0
    hits: int = 0
    hints: int = 0
    ai_shots: int = 0
    ai_hits: int = 0
    coach_opt: int = 0
    coach_total: int = 0
    streak: int = 0

    @property
    def accuracy(self) -> str:
        return "%d%%" % (100 * self.hits // self.shots) if self.shots else "-"

    def as_dict(self) -> Dict[str, int]:
        return {"shots": self.shots, "hits": self.hits, "hints": self.hints,
                "ai_shots": self.ai_shots, "ai_hits": self.ai_hits,
                "coach_opt": self.coach_opt, "coach_total": self.coach_total,
                "streak": self.streak}

    @classmethod
    def from_dict(cls, d: Dict[str, int]) -> "GameStats":
        return cls(**{k: int(d.get(k, 0)) for k in
                       ("shots", "hits", "hints", "ai_shots", "ai_hits",
                        "coach_opt", "coach_total", "streak")})


class PlacementCache:
    """Highly cohesive cache for every legal ship placement.

    Replaces the bare PLACEMENTS global + _rebuild_placements function
    with an explicit object. The module-level PLACEMENTS dict is kept
    as a compatibility view onto this cache.
    """

    def __init__(self) -> None:
        self._entries: Dict[int, list] = {}

    def rebuild(self, size: int, fleet_lengths: Sequence[int]) -> None:
        cache: Dict[int, list] = {}
        for length in sorted(set(fleet_lengths)):
            entries = []
            for horizontal in (True, False):
                for r in range(size):
                    for c in range(size):
                        cells = line_cells(r, c, length, horizontal)
                        if cells is not None:
                            entries.append((tuple(cells), frozenset(cells), horizontal))
            cache[length] = entries
        self._entries = cache
        global PLACEMENTS
        PLACEMENTS = cache

    def get(self, length: int) -> list:
        return self._entries.get(length, [])

    def __contains__(self, length: int) -> bool:
        return length in self._entries


_PLACEMENT_CACHE = PlacementCache()


class PeerRegistry:
    """Extracted from LANClient (God-class split): owns peer state."""

    def __init__(self) -> None:
        self._peers: Dict[str, Any] = {}
        self._lock = threading.Lock()

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._peers)

    def snapshot(self) -> List[Any]:
        with self._lock:
            return sorted(self._peers.values(),
                          key=lambda p: (p.name.lower(), p.id))

    # internal hooks used by LANClient facade (kept private)
    def _get_map(self) -> Dict[str, Any]:
        return self._peers

    def _get_lock(self):  # type: ignore[no-untyped-def]
        return self._lock


class ChatLog:
    """Extracted from LANClient: owns bounded chat history."""

    def __init__(self, limit: int = 200) -> None:
        self._history: collections.deque = collections.deque(maxlen=limit)

    def add(self, line: str) -> str:
        stamp = time.strftime("%H:%M")
        full = "[%s] %s" % (stamp, line)
        self._history.append(full)
        return full

    @property
    def messages(self) -> List[str]:
        return list(self._history)

    def recent(self, n: int = 50) -> List[str]:
        return list(self._history)[-n:]


class TcpRateLimiter:
    """Extracted from LANClient: owns per-IP connection throttling."""

    def __init__(self, max_hits: int = 10, window: float = 5.0) -> None:
        self._hits: Dict[str, List[float]] = {}
        self._lock = threading.Lock()
        self._max = max_hits
        self._window = window

    def allow(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(ip, []) if now - t < self._window]
            if len(recent) >= self._max:
                self._hits[ip] = recent
                return False
            recent.append(now)
            self._hits[ip] = recent
            return True


class DensityEngine:
    """Cohesive probability-map engine (extracted from bare functions)."""

    @staticmethod
    def scores(k: Any, targeting: bool) -> Tuple[list, int]:
        return _density(k, targeting)

    @staticmethod
    def coverage(k: Any) -> dict:
        return _coverage(k)


try:
    import readline  # noqa: F401  (arrow-key history on POSIX)
except ImportError:
    pass

# ----------------------------------------------------------------------------
# Board / fleet configuration (all runtime-tunable via configure_board)
# ----------------------------------------------------------------------------

SIZE = 10
COLS = "ABCDEFGHIJ"
FLEET = [("Carrier", 5), ("Battleship", 4), ("Cruiser", 3), ("Submarine", 3), ("Destroyer", 2)]
SHIP_LEN = dict(FLEET)
FLEET_LENGTHS = [n for _, n in FLEET]

FLEET_PRESETS = {
    "classic": [
        ("Carrier", 5), ("Battleship", 4), ("Cruiser", 3),
        ("Submarine", 3), ("Destroyer", 2),
    ],
    "small": [
        ("Cruiser", 3), ("Submarine", 2), ("Destroyer", 2),
    ],
    "armada": [
        ("Carrier", 5), ("Battleship", 4), ("Cruiser", 3),
        ("Submarine", 3), ("Destroyer", 3), ("Frigate", 2), ("Patrol", 2),
    ],
}

SETUP_PRESETS = [
    ("Standard — 10×10, classic 5-ship fleet", 10, "classic"),
    ("Skirmish —  8×8, small 3-ship fleet", 8, "small"),
    ("Grand    — 12×12, armada 7-ship fleet", 12, "armada"),
]


def configure_board(size: int, fleet_name: str) -> None:
    """Runtime-tunable board setup. Keeps module globals in sync with _CONFIG."""
    global SIZE, COLS, FLEET, SHIP_LEN, FLEET_LENGTHS
    size = max(6, min(14, int(size)))
    if fleet_name not in FLEET_PRESETS:
        fleet_name = "classic"
    fleet = [tuple(x) for x in FLEET_PRESETS[fleet_name]]
    max_len = max(n for _, n in fleet)
    if max_len > size:
        raise ValueError("ship length %d doesn't fit on a %d×%d board" % (max_len, size, size))
    _CONFIG.size = size
    _CONFIG.cols = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[:size]
    _CONFIG.fleet = fleet
    SIZE = _CONFIG.size
    COLS = _CONFIG.cols
    FLEET = _CONFIG.fleet
    SHIP_LEN = _CONFIG.ship_len
    FLEET_LENGTHS = _CONFIG.fleet_lengths
    _rebuild_placements()


# ----------------------------------------------------------------------------
# Terminal helpers
# ----------------------------------------------------------------------------

USE_COLOR = False
ANSI_OK = False
CODES = {
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "cyan": "36", "white": "97", "grey": "90", "bold": "1"
}

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def paint(text: str, *styles: str) -> str:
    if not is_color_enabled() or not styles:
        return text
    return "\033[%sm%s\033[0m" % (";".join(CODES[s] for s in styles), text)


def cursor_reverse(text: str) -> str:
    if is_ansi_ok():
        return "\033[7m%s\033[0m" % strip_ansi(text)
    return "[" + strip_ansi(text) + "]"


def clear():
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def supports_cursor_ui():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Visual settings
# ----------------------------------------------------------------------------
ESC = chr(27)
BRK = chr(91)
CR = chr(13)
REV_ON = ESC + BRK + "7m" + ESC + BRK + "1m"
REV_OFF = ESC + BRK + "0m"
SCREEN_FLASH_ON = ESC + BRK + "?5h"
SCREEN_FLASH_OFF = ESC + BRK + "?5l"

VISUAL_DEFAULTS = {
    "animations": True,
    "explosions": True,
    "shot_trails": True,
    "sunk_reveal": True,
    "animated_water": True,
    "last_shot_highlight": True,
    "fleet_status": True,
    "turn_banners": True,
    "radar_spinner": True,
    "victory_cinematics": True,
    "color_density": True,
    "screen_flash": False,
    "terminal_bell": False,
    "screen_shake": True,
    "hit_stop": True,
    "kill_cam": True,
    "damage_fire": True,
    "sonar_sweep": True,
    "captain_taunts": True,
    "streaks": True,
    "epic_mode": False,
}
VISUAL = dict(VISUAL_DEFAULTS)
_VISUAL_SETTINGS = VisualSettings(flags=VISUAL)  # single owner; VISUAL is compat view

EPIC_TIMINGS = {
    False: {"hit_stop": 0.12, "sunk_stop": 0.25, "shake_frames": 3},
    True: {"hit_stop": 0.30, "sunk_stop": 0.80, "shake_frames": 6},
}


def vis(name: str) -> bool:
    return _VISUAL_SETTINGS.get(name, False)


def can_animate():
    return supports_cursor_ui() and vis("animations")


def _bell(kind="miss"):
    if not vis("terminal_bell") or not sys.stdout.isatty():
        return
    try:
        if kind == "sunk":
            sys.stdout.write(chr(7) + chr(7))
        else:
            sys.stdout.write(chr(7))
        sys.stdout.flush()
    except Exception:
        pass


def _screen_flash():
    if not (vis("screen_flash") and sys.stdout.isatty() and ANSI_OK):
        return
    try:
        sys.stdout.write(SCREEN_FLASH_ON)
        sys.stdout.flush()
        time.sleep(0.05)
        sys.stdout.write(SCREEN_FLASH_OFF)
        sys.stdout.flush()
    except Exception:
        pass


def _burst_frames(frames, styles, hold=0.35):
    if not can_animate():
        return
    try:
        width = max(_vis_len(f) for f in frames) + 6
        per = max(0.03, hold / max(1, len(frames)))
        for f in frames:
            styled = paint(f, *styles)
            pad = width - _vis_len(f)
            sys.stdout.write(CR + "  " + styled + " " * max(0, pad))
            sys.stdout.flush()
            time.sleep(per)
        sys.stdout.write(CR + " " * width + CR)
        sys.stdout.flush()
    except Exception:
        pass


def _hit_stop(sunk: bool) -> None:
    try:
        if not (vis("hit_stop") and can_animate()):
            return
        t = EPIC_TIMINGS[bool(vis("epic_mode"))]
        time.sleep(t["sunk_stop"] if sunk else t["hit_stop"])
    except Exception:
        pass


def _screen_shake(intensity: int = 1) -> None:
    try:
        if not (vis("screen_shake") and can_animate()):
            return
        n = EPIC_TIMINGS[bool(vis("epic_mode"))]["shake_frames"] if intensity > 1 else 3
        for _ in range(n):
            pad = " " * (random.randint(0, intensity * 2))
            sys.stdout.write("\r" + pad + "  * *" + pad)
            sys.stdout.flush()
            time.sleep(0.03)
        sys.stdout.write("\r" + " " * 20 + "\r")
        sys.stdout.flush()
    except Exception:
        pass


def _shot_trail(pos, opp=False):
    if not (vis("shot_trails") and can_animate()):
        return
    cell = cell_name(pos)
    styles = ("red", "bold") if opp else ("cyan", "bold")
    frames = [
        "  > " + cell,
        " >> " + cell,
        " >>> " + cell,
        " * " + cell,
    ]
    _burst_frames(frames, styles, 0.22)


def burst_banner(text, styles=("bold",), hold=0.35):
    if not (vis("turn_banners") and can_animate()):
        return
    spaced = "  ".join(text.split())
    line = "== " + spaced + " =="
    _burst_frames([line], styles, hold)


def water_char(r, c):
    if vis("animated_water") and USE_COLOR and sys.stdout.isatty():
        chars = ["~", "~", "=", "."]
        idx = (r * 7 + c * 13 + int(time.time() * 2.0)) % len(chars)
        return paint(chars[idx], "blue")
    return paint("~", "blue")


def _highlight(ch):
    if not vis("last_shot_highlight"):
        return ch
    if USE_COLOR:
        return REV_ON + strip_ansi(ch) + REV_OFF
    return ch


def show_sunk_reveal(player, enemy, ship_name, last_player=None):
    if not (vis("sunk_reveal") and can_animate()):
        return
    cells = enemy.ship_cells.get(ship_name)
    if not cells:
        return
    try:
        clear()
        print(render_boards(player, enemy, reveal=False,
                            last_player=last_player, reveal_cells=set(cells)))
        print()
        if vis("kill_cam"):
            step = ""
            for ch in "S U N K".split():
                step = (step + " " + ch).strip()
                try:
                    if can_animate():
                        sys.stdout.write("\r  " + paint(step, "green", "bold"))
                        sys.stdout.flush()
                        time.sleep(0.15 if vis("epic_mode") else 0.06)
                except Exception:
                    pass
            print()
            print("  " + paint("enemy %s destroyed — %d shots to kill" % (ship_name, len(cells)), "green"))
            print()
        for line in big_banner("S H I P   S U N K", "green"):
            print("  " + line)
        time.sleep(0.8)
    except Exception:
        pass


def finish_cinematic(won):
    if not (vis("victory_cinematics") and can_animate()):
        return
    if won:
        frames = ["*", "+", "*", " * + * "]
        _burst_frames(frames, ("green", "bold"), 0.7)
    else:
        frames = ["~", "=", "v", "#v#"]
        _burst_frames(frames, ("red", "bold"), 0.7)


def visual_settings_menu():
    items = [
        ("Master animations", "animations"),
        ("Explosion / splash frames", "explosions"),
        ("Shot trails", "shot_trails"),
        ("Sunk ship reveal", "sunk_reveal"),
        ("Animated water", "animated_water"),
        ("Strong last-shot highlight", "last_shot_highlight"),
        ("Fleet status labels", "fleet_status"),
        ("Turn banners", "turn_banners"),
        ("Radar spinner", "radar_spinner"),
        ("Victory/defeat cinematics", "victory_cinematics"),
        ("Colorized density map", "color_density"),
        ("Screen flash", "screen_flash"),
        ("Terminal bell", "terminal_bell"),
        ("Screen shake on hits", "screen_shake"),
        ("Hit-stop pause", "hit_stop"),
        ("Sunk kill-cam", "kill_cam"),
        ("Burning damaged ships", "damage_fire"),
        ("Sonar sweep on enemy turn", "sonar_sweep"),
        ("Captain taunts", "captain_taunts"),
        ("Streak callouts", "streaks"),
        ("EPIC MODE (all Hollywood, slower)", "epic_mode"),
    ]

    sel = 0
    while True:
        options = []
        for label, key in items:
            state = paint("ON ", "green", "bold") if VISUAL.get(key) else paint("OFF", "red")
            options.append("%-32s %s" % (label, state))

        options.append("Reset to defaults")
        options.append("Back")

        idx = select_menu(paint("VISUAL SETTINGS", "bold"), options, start_idx=sel)
        sel = idx

        if idx == len(items) + 1:
            return

        if idx == len(items):
            _VISUAL_SETTINGS.reset()
            continue

        _, key = items[idx]
        _VISUAL_SETTINGS.toggle(key)


# ----------------------------------------------------------------------------
# Box drawing
# ----------------------------------------------------------------------------

BOX_TL, BOX_TR, BOX_BL, BOX_BR = "╭", "╮", "╰", "╯"
BOX_H, BOX_V = "─", "│"
DBOX_TL, DBOX_TR, DBOX_BL, DBOX_BR = "╔", "╗", "╚", "╝"
DBOX_H, DBOX_V = "═", "║"


def _vis_len(s):
    return len(strip_ansi(s))


def _pad_vis(s, width, align="left"):
    v = _vis_len(s)
    if v >= width:
        return s
    pad = width - v
    if align == "left":
        return s + " " * pad
    if align == "right":
        return " " * pad + s
    left = pad // 2
    right = pad - left
    return " " * left + s + " " * right


def box_top_line(inner_width, title=None, double=False):
    tl, tr, h = (DBOX_TL, DBOX_TR, DBOX_H) if double else (BOX_TL, BOX_TR, BOX_H)
    if not title:
        return tl + h * (inner_width + 2) + tr
    t = " " + title + " "
    tv = _vis_len(t)
    remaining = inner_width + 2 - tv
    if remaining < 0:
        return tl + t + tr
    left = remaining // 2
    right = remaining - left
    return tl + h * left + t + h * right + tr


def box_bottom_line(inner_width, double=False):
    bl, br, h = (DBOX_BL, DBOX_BR, DBOX_H) if double else (BOX_BL, BOX_BR, BOX_H)
    return bl + h * (inner_width + 2) + br


def box_content_line(content, inner_width):
    return BOX_V + " " + _pad_vis(content, inner_width) + " " + BOX_V


def side_by_side(left_lines, right_lines, gap="   "):
    w1 = max((_vis_len(x) for x in left_lines), default=0)
    w2 = max((_vis_len(x) for x in right_lines), default=0)
    h = max(len(left_lines), len(right_lines))
    out = []
    for i in range(h):
        a = left_lines[i] if i < len(left_lines) else ""
        b = right_lines[i] if i < len(right_lines) else ""
        out.append(_pad_vis(a, w1) + gap + _pad_vis(b, w2))
    return out


def boxed_panel(title, content_lines, double=False):
    inner = max((_vis_len(x) for x in content_lines), default=0)
    inner = max(inner, _vis_len(title) + 4)
    out = [box_top_line(inner, title, double)]
    for line in content_lines:
        out.append(box_content_line(line, inner))
    out.append(box_bottom_line(inner, double))
    return out


def title_banner():
    lines = [
        paint("B A T T L E S H I P S", "bold"),
        paint("~ Terminal Fleet Command ~", "cyan"),
    ]
    inner = max(_vis_len(t) for t in lines) + 6
    box = []
    box.append("╔" + "═" * inner + "╗")
    box.append("║" + " " * inner + "║")
    for text in lines:
        box.append("║" + _pad_vis(text, inner, "center") + "║")
    box.append("║" + " " * inner + "║")
    box.append("╚" + "═" * inner + "╝")
    return box


def big_banner(text, style="bold"):
    inner = _vis_len(text) + 6
    painted = paint(text, style)
    return [
        "╔" + "═" * inner + "╗",
        "║" + " " * inner + "║",
        "║" + _pad_vis(painted, inner, "center") + "║",
        "║" + " " * inner + "║",
        "╚" + "═" * inner + "╝",
    ]


def fleet_panel(board, current_name=None, title="FLEET"):
    NAME_W = 10
    rows = []
    for name, length in FLEET:
        if name == current_name:
            marker = paint("▶", "cyan", "bold")
        elif name in board.ship_cells:
            marker = paint("✓", "green", "bold")
        else:
            marker = " "
        if name in board.ship_cells:
            sil = paint("█" * length, "cyan")
        else:
            sil = paint("░" * length, "grey")
        pad = " " * max(0, NAME_W - len(name))
        rows.append("%s %s%s  %s" % (marker, name, pad, sil))
    return boxed_panel(paint(title, "bold"), rows)


class Spinner:
    # Logical order: __init__ -> private _run -> magic __enter__/__exit__ (magics last)
    def __init__(self, message: str = "Thinking", stream: Any = None) -> None:
        self.message = message
        self.stream = stream or sys.stdout
        self._stop = threading.Event()
        self._thread = None
        self._width = 0

    def __enter__(self):
        if not supports_cursor_ui():
            return self

        low = self.message.lower()
        if "enemy" in low or "opponent" in low or "aiming" in low:
            burst_banner("ENEMY TURN", ("red", "bold"), 0.28)

        try:
            if vis("sonar_sweep") and can_animate():
                for f in ["((.))", "((o))", "(((o)))"]:
                    self.stream.write("\r  sonar " + f)
                    self.stream.flush()
                    time.sleep(0.07)
                self.stream.write("\r" + " " * 20 + "\r")
                self.stream.flush()
        except Exception:
            pass

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=0.5)
        try:
            self.stream.write("\r" + " " * (self._width + 2) + "\r")
            self.stream.flush()
        except Exception:
            pass

    def _run(self):
        if vis("radar_spinner"):
            chars = "|/-" + chr(92)
        else:
            chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        i = 0
        while not self._stop.is_set():
            try:
                text = "  %s %s..." % (chars[i % len(chars)], self.message)
                self._width = len(text)
                self.stream.write("\r" + text)
                self.stream.flush()
            except Exception:
                return
            i += 1
            time.sleep(0.08)


def _burst_print(text, styles, hold):
    if not sys.stdout.isatty():
        print("  " + paint(text, *styles))
        return
    styled = paint(text, *styles)
    width = _vis_len(text) + 4
    try:
        sys.stdout.write("\r  " + styled)
        sys.stdout.flush()
        time.sleep(hold)
        sys.stdout.write("\r" + " " * width + "\r")
        sys.stdout.flush()
    except Exception:
        pass


def burst_shot(pos, hit, ship, sunk, opp=False, level="Easy"):
    cell = cell_name(pos)

    _hit_stop(bool(sunk))
    _screen_shake(2 if sunk else 1)

    _shot_trail(pos, opp=opp)

    if vis("explosions") and can_animate():
        if sunk:
            frames = [".", "*", "*", "* * *"]
            styles = ("red", "bold") if opp else ("green", "bold")
            _burst_frames(frames, styles, 0.45)
        elif hit:
            frames = [".", "*", "*"]
            styles = ("red",) if opp else ("yellow",)
            _burst_frames(frames, styles, 0.30)
        else:
            frames = [".", "o", "~"]
            styles = ("white",) if opp else ("blue",)
            _burst_frames(frames, styles, 0.22)

    if hit:
        _screen_flash()

    _bell("sunk" if sunk else ("hit" if hit else "miss"))

    try:
        if sunk and can_animate() and (vis("terminal_bell") or vis("epic_mode")) and sys.stdout.isatty():
            time.sleep(0.15 if vis("epic_mode") else 0.08)
            _bell("hit")
    except Exception:
        pass

    if opp:
        if sunk:
            text = "* * *   SHIP LOST   * * *   enemy sank your %s at %s" % (ship, cell)
            _burst_print(text, ("red", "bold"), hold=1.5)
        elif hit:
            text = "*  enemy HIT at %s  *" % cell
            _burst_print(text, ("red", "bold"), hold=0.9)
        else:
            text = "·  enemy missed at %s" % cell
            _burst_print(text, ("white",), hold=0.5)
    else:
        if sunk:
            text = "* * *   S U N K   * * *   enemy %s destroyed at %s" % (ship, cell)
            _burst_print(text, ("green", "bold"), hold=1.5)
        elif hit:
            text = "*  H I T  at %s  *" % cell
            _burst_print(text, ("yellow", "bold"), hold=0.9)
        else:
            text = "·  splash at %s" % cell
            _burst_print(text, ("blue",), hold=0.5)

    if not opp:
        try:
            _BURST_STREAK["n"] = _BURST_STREAK["n"] + 1 if hit else 0
        except Exception:
            pass
        if can_animate():
            try:
                if vis("streaks") and _BURST_STREAK["n"] == 2:
                    _burst_print("DOUBLE HIT!", ("yellow", "bold"), hold=0.6)
                elif vis("streaks") and _BURST_STREAK["n"] >= 3:
                    _burst_print("ON FIRE! x%d" % _BURST_STREAK["n"], ("red", "bold"), hold=0.6)
                if vis("captain_taunts") and random.random() < 0.35:
                    pool = TAUNTS.get(level, TAUNTS["Easy"]).get(
                        "sunk" if sunk else ("hit" if hit else "miss"), [])
                    if pool:
                        _burst_print("ENEMY CAPTAIN: %s" % random.choice(pool),
                                     ("white",), hold=0.5)
            except Exception:
                pass


def burst_shot_lan(pos, hit, sunk_len, opp=False, level="Easy"):
    cell = cell_name(pos)

    _hit_stop(bool(sunk_len))
    _screen_shake(2 if sunk_len else 1)

    _shot_trail(pos, opp=opp)

    if vis("explosions") and can_animate():
        if sunk_len:
            frames = [".", "*", "*", "* * *"]
            styles = ("red", "bold") if opp else ("green", "bold")
            _burst_frames(frames, styles, 0.45)
        elif hit:
            frames = [".", "*", "*"]
            styles = ("red",) if opp else ("yellow",)
            _burst_frames(frames, styles, 0.30)
        else:
            frames = [".", "o", "~"]
            styles = ("white",) if opp else ("blue",)
            _burst_frames(frames, styles, 0.22)

    if hit:
        _screen_flash()

    _bell("sunk" if sunk_len else ("hit" if hit else "miss"))

    if opp:
        if sunk_len:
            text = "* * *   LOST A SHIP   * * *   opponent hit %s (len %d)" % (cell, sunk_len)
            _burst_print(text, ("red", "bold"), hold=1.5)
        elif hit:
            text = "*  opponent HIT at %s" % cell
            _burst_print(text, ("red", "bold"), hold=0.9)
        else:
            text = "·  opponent missed at %s" % cell
            _burst_print(text, ("white",), hold=0.5)
    else:
        if sunk_len:
            text = "* * *   S U N K   * * *   enemy ship (len %d) down at %s" % (sunk_len, cell)
            _burst_print(text, ("green", "bold"), hold=1.5)
        elif hit:
            text = "*  H I T  at %s  *" % cell
            _burst_print(text, ("yellow", "bold"), hold=0.9)
        else:
            text = "·  splash at %s" % cell
            _burst_print(text, ("blue",), hold=0.5)


class Quit(Exception):
    pass


def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit


def pick(prompt, valid):
    while True:
        raw = ask(prompt).lower()
        if raw in valid:
            return raw
        print("  Enter one of: %s." % ", ".join(valid))


def confirm(prompt):
    return ask(prompt + " (y/n) > ").lower() in ("y", "yes")


# ----------------------------------------------------------------------------
# Raw single-key reader
# ----------------------------------------------------------------------------

class KeyReader:
    # Logical order: __init__ -> public get_key -> private _win/_posix -> magic __enter__/__exit__ last
    def __init__(self) -> None:
        self.windows = (os.name == "nt")
        self.fd = None
        self.saved = None

    def __enter__(self):
        if self.windows:
            return self
        try:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except Exception:
            self.saved = None
        return self

    def __exit__(self, *exc):
        if self.windows or self.saved is None:
            return
        try:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        except Exception:
            pass

    def get_key(self):
        try:
            if self.windows:
                return self._win()
            return self._posix()
        except (KeyboardInterrupt, EOFError):
            return "CTRL_C"

    def _win(self):
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b'\x00', b'\xe0'):
            ch2 = msvcrt.getch()
            return {
                b'H': "UP", b'P': "DOWN", b'M': "RIGHT", b'K': "LEFT",
                b'G': "HOME", b'O': "END", b'S': "DELETE",
            }.get(ch2, "")
        if ch in (b'\r', b'\n'):
            return "ENTER"
        if ch == b'\x1b':
            return "ESC"
        if ch in (b'\x08', b'\x7f'):
            return "BACKSPACE"
        if ch == b'\x03':
            return "CTRL_C"
        if ch == b'\x04':
            return "CTRL_D"
        if ch == b'\t':
            return "TAB"
        try:
            return ch.decode("utf-8", "ignore")
        except Exception:
            return ""

    def _posix(self):
        import select
        try:
            b = os.read(self.fd, 1)
        except OSError:
            return "EOF"
        if not b:
            return "EOF"

        if b == b'\x1b':
            r, _, _ = select.select([self.fd], [], [], 0.03)
            if not r:
                return "ESC"
            b2 = os.read(self.fd, 1)
            if b2 == b'[':
                seq = b'['
                while True:
                    r, _, _ = select.select([self.fd], [], [], 0.03)
                    if not r:
                        break
                    nxt = os.read(self.fd, 1)
                    seq += nxt
                    if nxt.isalpha() or nxt == b'~':
                        break
                return {
                    b'[A': "UP", b'[B': "DOWN", b'[C': "RIGHT", b'[D': "LEFT",
                    b'[H': "HOME", b'[F': "END",
                    b'[1~': "HOME", b'[4~': "END", b'[3~': "DELETE",
                    b'[5~': "PGUP", b'[6~': "PGDN",
                }.get(seq, "")
            if b2 == b'O':
                nxt = os.read(self.fd, 1)
                return {
                    b'OA': "UP", b'OB': "DOWN", b'OC': "RIGHT", b'OD': "LEFT",
                    b'OH': "HOME", b'OF': "END",
                }.get(b'O' + nxt, "")
            return ""

        if b in (b'\r', b'\n'):
            return "ENTER"
        if b in (b'\x7f', b'\x08'):
            return "BACKSPACE"
        if b == b'\x03':
            return "CTRL_C"
        if b == b'\x04':
            return "CTRL_D"
        if b == b'\t':
            return "TAB"

        buf = b
        while True:
            try:
                return buf.decode("utf-8")
            except UnicodeDecodeError:
                r, _, _ = select.select([self.fd], [], [], 0.03)
                if not r:
                    return ""
                buf += os.read(self.fd, 1)


def select_menu(header, options, allow_quit=False, footer="", start_idx=0):
    if not supports_cursor_ui():
        print(header)
        for i, opt in enumerate(options, 1):
            print("  %d) %s" % (i, opt))
        while True:
            raw = ask("Choose 1-%d > " % len(options))
            if allow_quit and raw.lower() in ("q", "quit"):
                return -1
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            print("  Enter one of: 1-%d." % len(options))

    idx = max(0, min(start_idx, len(options) - 1)) if options else 0
    with KeyReader() as kr:
        while True:
            clear()
            print(header)
            print()
            for i, opt in enumerate(options):
                if i == idx:
                    line = "  " + paint("▶ %d) %s" % (i + 1, opt), "cyan", "bold")
                else:
                    line = "    %d) %s" % (i + 1, opt)
                print(line)
            print()
            print(paint("  " + "─" * 46, "grey"))
            hint = "  ↑/↓ move · Enter select · 1-%d jump" % len(options)
            if allow_quit:
                hint += " · Q quit"
            if footer:
                hint += " · " + footer
            print(hint)
            key = kr.get_key()
            if key == "UP":
                idx = (idx - 1) % len(options)
            elif key == "DOWN":
                idx = (idx + 1) % len(options)
            elif key == "ENTER":
                return idx
            elif key == "CTRL_C":
                raise Quit
            elif key and len(key) == 1 and key in "123456789":
                n = int(key)
                if 1 <= n <= len(options):
                    return n - 1
            elif allow_quit and key in ("Q", "q", "ESC"):
                return -1


# ----------------------------------------------------------------------------
# Coordinates (dynamic board size)
# ----------------------------------------------------------------------------

def cell_name(pos: Cell) -> str:
    return "%s%d" % (COLS[pos[1]], pos[0] + 1)


def parse_cell(text: str) -> Optional[Cell]:
    t = text.strip().upper().replace("-", " ").replace(",", " ")
    parts = t.split()

    if len(parts) == 1:
        s = parts[0]
        i = 0
        while i < len(s) and s[i].isalpha():
            i += 1
        if 0 < i < len(s) and s[i:].isdigit():
            col_str, row_str = s[:i], s[i:]
            if col_str in COLS:
                row = int(row_str)
                if 1 <= row <= SIZE:
                    return row - 1, COLS.index(col_str)
        i = 0
        while i < len(s) and s[i].isdigit():
            i += 1
        if 0 < i < len(s) and s[i:].isalpha():
            row_str, col_str = s[:i], s[i:]
            if col_str in COLS:
                row = int(row_str)
                if 1 <= row <= SIZE:
                    return row - 1, COLS.index(col_str)
        return None

    if len(parts) == 2:
        a, b = parts
        if a.isalpha() and b.isdigit() and a in COLS:
            row = int(b)
            if 1 <= row <= SIZE:
                return row - 1, COLS.index(a)
        if b.isalpha() and a.isdigit() and b in COLS:
            row = int(a)
            if 1 <= row <= SIZE:
                return row - 1, COLS.index(b)
        return None

    return None


def shot_error(text):
    t = text.strip().upper().replace("-", " ").replace(",", " ")
    m = re.match(r"^\s*([A-Z]+)\s*(\d+)\s*$", t) or re.match(r"^\s*(\d+)\s*([A-Z]+)\s*$", t)
    if m:
        a, b = m.group(1), m.group(2)
        col = a if a.isalpha() else b
        row = b if a.isalpha() else a
        if col not in COLS:
            return "columns run %s–%s" % (COLS[0], COLS[-1])
        try:
            rn = int(row)
        except ValueError:
            return "columns run %s–%s" % (COLS[0], COLS[-1])
        if not 1 <= rn <= SIZE:
            return "rows run 1–%d" % SIZE
    example_col = COLS[1] if len(COLS) > 1 else COLS[0]
    return "Can't read that. Use a column letter %s-%s plus a row number 1-%d, like %s2." % (
        COLS[0], COLS[-1], SIZE, example_col)


def parse_placement(text):
    t = text.strip()
    if not t:
        return None
    s = t.upper().replace("-", " ").replace(",", " ")
    parts = s.split()
    if not parts:
        return None

    if len(parts) == 1:
        tok = parts[0]
        m = re.match(r"^([A-Z])(\d+)([HVDA])([A-Z]*)$", tok)
        if m:
            col, row_s, d, _ = m.groups()
        else:
            m = re.match(r"^(\d+)([A-Z])([HVDA])([A-Z]*)$", tok)
            if not m:
                return None
            row_s, col, d, _ = m.groups()
        row = int(row_s)
        if col not in COLS or not (1 <= row <= SIZE):
            return None
        return (row - 1, COLS.index(col)), d == "H"

    direction = None
    cell_parts = []
    for tok in parts:
        if tok in ("H", "HORIZONTAL", "HORIZ"):
            direction = True
        elif tok in ("V", "VERTICAL", "VERT"):
            direction = False
        elif tok in ("D", "DOWN"):
            direction = False
        elif tok in ("A", "ACROSS"):
            direction = True
        else:
            cell_parts.append(tok)

    if direction is None or not cell_parts:
        return None
    pos = parse_cell(" ".join(cell_parts))
    if pos is None:
        return None
    return pos, direction


def line_cells(r: int, c: int, length: int, horizontal: bool) -> Optional[List[Cell]]:
    cells = [(r, c + i) if horizontal else (r + i, c) for i in range(length)]
    er, ec = cells[-1]
    if er >= SIZE or ec >= SIZE:
        return None
    return cells


# ----------------------------------------------------------------------------
# Placement cache (rebuilt whenever the board or fleet changes)
# ----------------------------------------------------------------------------

PLACEMENTS = {}


def _rebuild_placements() -> None:
    """Precompute every legal ship placement for the current board.

    Each entry is (cells_tuple, cells_frozenset, horizontal). The tuple is
    used for iteration; the frozenset is used for fast disjointness tests.
    Delegates to the cohesive PlacementCache singleton.
    """
    _PLACEMENT_CACHE.rebuild(SIZE, FLEET_LENGTHS)


_rebuild_placements()


def inside(p: Cell) -> bool:
    return 0 <= p[0] < SIZE and 0 <= p[1] < SIZE


def neighbors(p: Cell):
    r, c = p
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        q = (r + dr, c + dc)
        if inside(q):
            yield q


# ----------------------------------------------------------------------------
# Domain: Board (fleet state) — defined before Knowledge/AI/Game that use it
# ----------------------------------------------------------------------------

class Board:
    # Logical order: __init__ -> public actions -> queries -> properties -> privates
    def __init__(self, fleet: Optional[Sequence[Tuple[str, int]]] = None) -> None:
        self.fleet = list(fleet) if fleet is not None else list(FLEET)
        self.cells = [[None] * SIZE for _ in range(SIZE)]
        self.ship_cells = {}
        self.shots = {}
        self.order = []

    def check_placement(self, length, r, c, horizontal):
        cells = line_cells(r, c, length, horizontal)
        if cells is None:
            return None, "it runs off the board"
        for cr, cc in cells:
            if self.cells[cr][cc]:
                return None, "it overlaps your %s" % self.cells[cr][cc]
        return cells, None

    def place(self, name, cells):
        for r, c in cells:
            self.cells[r][c] = name
        self.ship_cells[name] = cells
        self.order.append(name)

    def undo(self):
        if not self.order:
            return None
        name = self.order.pop()
        for r, c in self.ship_cells.pop(name):
            self.cells[r][c] = None
        return name

    def place_randomly(self, ships, rng=random, tries=1000):
        for name, length in ships:
            for _ in range(tries):
                cells, err = self.check_placement(
                    length, rng.randrange(SIZE), rng.randrange(SIZE), rng.random() < 0.5)
                if not err:
                    self.place(name, cells)
                    break
            else:
                raise ValueError("cannot place %s (length %d) after %d tries" % (name, length, tries))

    def place_contrarian(self, ships, K=32, rng=random):
        base = Knowledge()
        base_scores = density(base)
        best_cells, best_score = None, None
        for _ in range(K):
            tmp = Board()
            tmp.place_randomly(ships, rng=rng)
            s = sum(base_scores[r][c] for cells in tmp.ship_cells.values() for (r, c) in cells)
            if best_score is None or s < best_score:
                best_score, best_cells = s, {n: list(c) for n, c in tmp.ship_cells.items()}
        for n, cells in best_cells.items():
            self.place(n, cells)

    def fire(self, pos):
        assert pos not in self.shots, "double fire at %r" % (pos,)
        ship = self.cells[pos[0]][pos[1]]
        self.shots[pos] = ship is not None
        return ship is not None, ship, bool(ship) and self.is_sunk(ship)

    def is_sunk(self, name):
        return all(p in self.shots for p in self.ship_cells[name])

    # -- Public queries ---------------------------------------------------
    def afloat(self) -> List[str]:
        return [n for n, _ in self.fleet if n in self.ship_cells and not self.is_sunk(n)]

    def all_sunk(self) -> bool:
        return not self.afloat()

    # -- Properties (computed, read-only views) -------------------------------
    @property
    def ship_count(self) -> int:
        return len(self.ship_cells)

    @property
    def shot_count(self) -> int:
        return len(self.shots)

    @property
    def sunk_count(self) -> int:
        return sum(1 for n in self.ship_cells if self.is_sunk(n))


# ----------------------------------------------------------------------------
# Domain: Knowledge (shooter belief state)
# ----------------------------------------------------------------------------

class Knowledge:
    # Logical order: __init__ -> public record/query -> properties -> classmethod -> privates
    def __init__(self, fleet_lengths: Optional[Sequence[int]] = None) -> None:
        self.remaining = list(fleet_lengths) if fleet_lengths is not None else list(FLEET_LENGTHS)
        self.miss = set()
        self.hit = set()
        self.sunk = set()
        self._hit_time = {}
        self._sinkings = []
        self._shots = 0

    def tried(self, p: Cell) -> bool:
        return p in self.miss or p in self._hit_time

    def untried(self) -> List[Cell]:
        return [(r, c) for r in range(SIZE) for c in range(SIZE) if not self.tried((r, c))]

    @property
    def shot_count(self) -> int:
        """Total shots recorded (encapsulated view of _shots)."""
        return self._shots

    @property
    def hits(self) -> Set[Cell]:
        return set(self.hit)

    @property
    def misses(self) -> Set[Cell]:
        return set(self.miss)

    @classmethod
    def from_board(cls, enemy_board: Any, fleet_lengths: Optional[Sequence[int]] = None) -> "Knowledge":  # alternative constructor (public, right after __init__)
        k = cls(fleet_lengths=fleet_lengths)
        shots = enemy_board.shots

        for pos, hit in shots.items():
            if not hit:
                k.record(pos, False, None)

        sunk_ships = [n for n, cells in enemy_board.ship_cells.items()
                      if all(q in shots for q in cells)]

        for name in sunk_ships:
            cells = list(enemy_board.ship_cells[name])
            for pos in cells[:-1]:
                k.record(pos, True, None)
            k.record(cells[-1], True, len(cells))

        sunk_cells = {q for n in sunk_ships for q in enemy_board.ship_cells[n]}
        for pos, hit in shots.items():
            if hit and pos not in sunk_cells:
                k.record(pos, True, None)
        return k


    def record(self, pos, hit, sunk_len=None):
        self._shots += 1
        if not hit:
            self.miss.add(pos)
            return

        self._hit_time[pos] = self._shots
        if sunk_len:
            self.remaining.remove(sunk_len)
            self._sinkings.append((pos, sunk_len))

        if self._sinkings:
            self._refresh()
        else:
            self.hit.add(pos)

    def _refresh(self):
        options = self._assignments()
        self.sunk = set(frozenset.intersection(*options)) if options else set()
        self.hit = set(self._hit_time) - self.sunk


    @staticmethod
    def _lines_through(pos, length, ok):
        r, c = pos
        lines = []
        for dr, dc in ((0, 1), (1, 0)):
            for k in range(length):
                cells = [(r + dr * (i - k), c + dc * (i - k)) for i in range(length)]
                if all(inside(p) and ok(p) for p in cells):
                    lines.append(cells)
        return lines

    def _assignments(self):
        results = set()

        def place(i, used):
            if i == len(self._sinkings):
                if self._leftover_ok(used):
                    results.add(used)
                return

            pos, length = self._sinkings[i]
            when = self._hit_time[pos]
            for line in self._lines_through(
                    pos, length, lambda q: self._hit_time.get(q, when + 1) <= when):
                if used.isdisjoint(line):
                    place(i + 1, used | frozenset(line))

        place(0, frozenset())
        return list(results)

    def _leftover_ok(self, covered):
        blocked = self.miss | covered
        leftover = [p for p in self._hit_time if p not in covered]

        def explain(cells, ships, used):
            cells = [p for p in cells if p not in used]
            if not cells:
                return True
            for i, length in enumerate(ships):
                if length in ships[:i]:
                    continue
                for line in self._lines_through(
                        cells[0], length, lambda q: q not in blocked and q not in used):
                    if explain(cells, ships[:i] + ships[i + 1:], used | frozenset(line)):
                        return True
            return False

        return explain(leftover, list(self.remaining), frozenset())


def _density(k, targeting):
    scores = [[0] * SIZE for _ in range(SIZE)]
    total = 0
    blocked = k.miss | k.sunk
    hit = k.hit

    for length in k.remaining:
        for cells, cells_set, _ in PLACEMENTS[length]:
            if not cells_set.isdisjoint(blocked):
                continue
            covered = 0
            for p in cells:
                if p in hit:
                    covered += 1
            if targeting and not covered:
                continue
            weight = 10 ** covered
            total += weight
            for (r, c) in cells:
                if (r, c) not in hit:
                    scores[r][c] += weight
    return scores, total


def density(k):
    scores, total = _density(k, bool(k.hit))
    if not total and k.hit:
        scores, total = _density(k, False)
    return scores


def _coverage(k):
    cov = {}
    blocked = k.miss | k.sunk
    hit = k.hit
    targeting = bool(hit)

    for length in k.remaining:
        for cells, cells_set, horizontal in PLACEMENTS[length]:
            if not cells_set.isdisjoint(blocked):
                continue

            non_hits = []
            for p in cells:
                if p not in hit:
                    non_hits.append(p)

            covered = len(cells) - len(non_hits)
            if targeting and not covered:
                continue

            weight = 10 ** covered
            for p in non_hits:
                e = cov.get(p)
                if e is None:
                    e = {"score": 0, "count": 0, "h": 0, "v": 0, "per_len": {}}
                    cov[p] = e
                e["score"] += weight
                e["count"] += 1
                if horizontal:
                    e["h"] += 1
                else:
                    e["v"] += 1
                e["per_len"][length] = e["per_len"].get(length, 0) + 1

    for p in k.untried():
        if p not in cov:
            cov[p] = {"score": 0, "count": 0, "h": 0, "v": 0, "per_len": {}}
    return cov


def _variance(vals):
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((v - m) ** 2 for v in vals) / len(vals)


def top_candidates(k, n=3, rng=random):
    cov = _coverage(k)
    cells = k.untried()
    scored = []
    for p in cells:
        e = cov[p]
        per_len = e["per_len"]
        m = len(per_len)
        if m == 0:
            var = 0.0
        else:
            vals = list(per_len.values())
            mean = sum(vals) / m
            var = sum((x - mean) ** 2 for x in vals) / m
        scored.append((p, e["score"], e["count"], var))
    scored.sort(key=lambda t: (-t[1], -t[3], t[0]))
    return [(p, s, c) for (p, s, c, _) in scored[:n]]


def density_digits(k):
    scores = density(k)
    mx = max((scores[r][c] for r in range(SIZE) for c in range(SIZE)), default=0)
    out = [[0] * SIZE for _ in range(SIZE)]
    for r in range(SIZE):
        for c in range(SIZE):
            out[r][c] = (9 * scores[r][c] // mx) if mx else 0
    return out


def best_cell(k, rng=random):
    cov = _coverage(k)
    cells = k.untried()
    if not cells:
        return None

    top_score = -1
    for p in cells:
        s = cov[p]["score"]
        if s > top_score:
            top_score = s

    tied = [p for p in cells if cov[p]["score"] == top_score]
    if len(tied) == 1:
        return tied[0]

    # Compute each candidate's variance exactly once.
    scored = []
    for p in tied:
        per_len = cov[p]["per_len"]
        n = len(per_len)
        if n == 0:
            scored.append((0.0, p))
            continue
        vals = list(per_len.values())
        m = sum(vals) / n
        v = sum((x - m) ** 2 for x in vals) / n
        scored.append((v, p))

    scored.sort(key=lambda t: (-t[0], t[1]))
    best_var = scored[0][0]
    best = [p for (v, p) in scored if v == best_var]
    return rng.choice(best)


def _density_char(d):
    if not (vis("color_density") and USE_COLOR):
        return str(d)

    if d <= 0:
        return paint(".", "grey")
    if d <= 2:
        return paint(str(d), "blue")
    if d <= 4:
        return paint(str(d), "cyan")
    if d <= 6:
        return paint(str(d), "green")
    if d <= 8:
        return paint(str(d), "yellow")

    return paint(str(d), "red", "bold")


def render_density(k):
    digits = density_digits(k)
    rows = ["    " + " ".join(COLS)]

    for r in range(SIZE):
        row = []
        for c in range(SIZE):
            if k.tried((r, c)):
                row.append("X" if (r, c) in k.hit or (r, c) in k.sunk else "o")
            else:
                row.append(_density_char(digits[r][c]))

        rows.append("%2d  " % (r + 1) + " ".join(row))

    return "\n".join(boxed_panel(paint("DENSITY MAP", "bold"), rows))


def hint_text(k, rng=random):
    top = top_candidates(k, n=3, rng=rng)
    lines = []
    for i, (p, score, count) in enumerate(top, 1):
        why = "covered by %d remaining placements" % count
        lines.append("%d) %s score=%d (%s)" % (i, cell_name(p), score, why))
    if len(top) > 1 and top[0][1] == top[1][1]:
        lines.append("Tie on score — tie-break prefers highest coverage variance, then random.")
    return "\n".join(boxed_panel(paint("HINT", "bold"), lines))


def expert_par(ship_cells, rng=random):
    truth = Board()
    for name, cells in ship_cells.items():
        truth.place(name, list(cells))

    ai = ExpertAI(rng=rng)
    shots = 0
    while not truth.all_sunk() and shots < 1000:
        pos = ai.choose()
        hit, ship, sunk = truth.fire(pos)
        ai.record(pos, hit, SHIP_LEN[ship] if sunk else None)
        shots += 1
    return shots


def par_line(shots, par):
    return "You won in %d shots. Expert par: %d." % (shots, par)


# 10 lines per level: 3 hit + 3 miss + 2 sunk + 2 losing. Short, naval,
# no profanity, no position leaks. The "losing" pool is data for now;
# only hit/miss/sunk are triggered in V1 (burst_shot has no board context
# to detect "losing" — follow-up wiring).
TAUNTS = {
    "Easy": {"hit": ["Ha! Lucky shot, admiral.", "Oops — nice one!", "Hey, that tickled."],
             "miss": ["Missed me!", "Splash! Try again.", "Is that fog or aim?"],
             "sunk": ["Hey! That was my favorite ship!", "No fair, I liked that one."],
             "losing": ["Uh oh...", "This is fine. Everything is fine."]},
    "Medium": {"hit": ["Good shooting.", "Noted.", "Copy that hit."],
               "miss": ["Wide.", "No damage.", "Splash out."],
               "sunk": ["You got one. Respect.", "Ship lost. My bad."],
               "losing": ["Tide is turning...", "Holding the line."]},
    "Hard": {"hit": ["Efficient.", "Copy that.", "Plotting return fire."],
             "miss": ["Wasted shell.", "Negative splash.", "Range off."],
             "sunk": ["Ship lost. Adjusting.", "Casualties logged."],
             "losing": ["Requesting reinforcements.", "Damage control parties out."]},
    "Expert": {"hit": ["Probability updated.", "Interesting line.", "Recalibrating."],
               "miss": ["Suboptimal.", "Expected.", "Within tolerance."],
               "sunk": ["Acceptable loss.", "Model updated."],
               "losing": ["Recalculating...", "Win probability falling."]},
    "Nightmare": {"hit": ["Logged.", "Noted.", "."],
                  "miss": ["No.", "Miss.", "Nothing."],
                  "sunk": ["...", "Irrelevant."],
                  "losing": ["...", "Still coming."]},
}

_BURST_STREAK = {"n": 0}


def _reset_burst_state() -> None:
    """Reset per-seat display streak. Call at game start and hotseat handoff."""
    try:
        _BURST_STREAK["n"] = 0
    except Exception:
        pass


def war_sparkline(history) -> str:
    seq = [("H" if e.get("hit") else ".") for e in history[-20:]]
    return "".join(seq) or "-"


def mvp_line(game) -> str:
    best = None
    for e in getattr(game, "shot_history", []):
        if e.get("sunk"):
            best = e
            break
    if best is None:
        return "MVP: none yet"
    return "MVP: %s (sunk %s)" % (cell_name(best["pos"]), best.get("ship") or "ship")


def bench_solo(ai_cls, games, seed):
    shots = []
    for g in range(games):
        truth = Board()
        truth.place_randomly(FLEET, rng=random.Random("bench-%s-layout-%d" % (seed, g)))
        ai = ai_cls(rng=random.Random("bench-%s-ai-%d" % (seed, g)))
        n = 0
        while not truth.all_sunk() and n < 1000:
            pos = ai.choose()
            hit, ship, sunk = truth.fire(pos)
            ai.record(pos, hit, SHIP_LEN[ship] if sunk else None)
            n += 1
        shots.append(n)
    return shots


def bench_match(clsA, clsB, seed):
    ta, tb = Board(), Board()
    ta.place_randomly(FLEET, rng=random.Random("match-%s-fa" % seed))
    tb.place_randomly(FLEET, rng=random.Random("match-%s-fb" % seed))

    a, b = clsA(rng=random.Random("match-%s-a" % seed)), clsB(rng=random.Random("match-%s-b" % seed))
    na = nb = 0

    while True:
        pos = a.choose()
        hit, ship, sunk = tb.fire(pos)
        a.record(pos, hit, SHIP_LEN[ship] if sunk else None)
        na += 1
        if tb.all_sunk():
            return 0, na, nb

        pos = b.choose()
        hit, ship, sunk = ta.fire(pos)
        b.record(pos, hit, SHIP_LEN[ship] if sunk else None)
        nb += 1
        if ta.all_sunk():
            return 1, na, nb


SAVE_VERSION = 1  # NOTE: load_game() resolves LEVELS/Game lazily at call time (defined below)


def board_to_dict(board):
    return {
        "ships": {n: [list(p) for p in cells] for n, cells in board.ship_cells.items()},
        "shots": [[list(p), hit] for p, hit in board.shots.items()],
        "order": list(board.order),
    }


def board_from_dict(d):
    try:
        b = Board()
        for name, cells in d["ships"].items():
            b.place(name, [tuple(p) for p in cells])
        for p, hit in d["shots"]:
            b.shots[tuple(p)] = bool(hit)
        b.order = list(d.get("order", []))
        return b
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError("bad save: board: %s" % e)


def _encode_state(state):
    ver, inner, gauss = state
    return [ver, list(inner), gauss]


def _decode_state(doc):
    ver, inner, gauss = doc
    return (ver, tuple(inner), gauss)


def save_game(game, path):
    doc = {
        "version": SAVE_VERSION,
        "level": game.level,
        "mode": getattr(game, "mode", "single"),
        "turn": game.turn,
        "stats": dict(game.stats),
        "player": board_to_dict(game.player),
        "enemy": board_to_dict(game.enemy),
        "random_state": _encode_state(random.getstate()),
        "ai_state": (_encode_state(game.ai.rng.getstate())
                     if getattr(game.ai, "rng", None) is not None
                     and game.ai.rng is not random else None),
    }
    with open(path, "w") as f:
        json.dump(doc, f)


def load_game(path):
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ValueError("bad save: %s" % e)

    try:
        if doc["version"] != SAVE_VERSION:
            raise ValueError("bad save: version %r" % (doc.get("version"),))

        level = doc["level"]
        cls = dict((n, c) for n, c, _ in LEVELS)[level]
        game = Game(level, cls, mode=doc.get("mode", "single"))
        game.turn = int(doc["turn"])
        game.stats = dict(doc["stats"])
        game.player = board_from_dict(doc["player"])
        game.enemy = board_from_dict(doc["enemy"])
        game.pk = Knowledge.from_board(game.enemy)

        random.setstate(_decode_state(doc["random_state"]))
        if doc.get("ai_state") is not None and game.ai.rng is not random:
            game.ai.rng.setstate(_decode_state(doc["ai_state"]))
        return game
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("bad save: %s" % e)


def is_coach_opt(pk, pos):
    return pos in [p for (p, _, _) in top_candidates(pk, n=3)]


def coach_line(opt, total):
    if not total:
        return "Coach: no shots recorded."
    return "Coach: %d%% of your shots were in the expert top-3 (%d/%d)." % (
        100 * opt // total, opt, total)


def salvo_size(board):
    return len(board.afloat())


def salvo_has_duplicate(positions):
    return len(set(positions)) != len(positions)


def apply_salvo(enemy, pk, positions):
    out = []
    for pos in positions:
        hit, ship, sunk = enemy.fire(pos)
        pk.record(pos, hit, SHIP_LEN[ship] if sunk else None)
        out.append((hit, ship, sunk))
    return out


# ----------------------------------------------------------------------------
# Domain: AI opponents (depend on Board/Knowledge/Density — defined above)
# ----------------------------------------------------------------------------

class AI:
    # Logical order: __init__ -> public choose/record -> protected helpers
    def __init__(self, rng: Any = None) -> None:
        self.k = Knowledge()
        self.rng = rng or random

    def record(self, pos, hit, sunk_len=None):
        self.k.record(pos, hit, sunk_len)

    def choose(self):
        raise NotImplementedError

    def adjacent_to_hits(self):
        out = []
        for p in self.k.hit:
            out.extend(q for q in neighbors(p) if not self.k.tried(q))
        return out


class EasyAI(AI):
    def choose(self):
        return self.rng.choice(self.k.untried())


class MediumAI(AI):
    def choose(self):
        adj = self.adjacent_to_hits()
        return self.rng.choice(adj) if adj else self.rng.choice(self.k.untried())


class HardAI(AI):
    def line_extensions(self):
        k, ext = self.k, []
        for (r, c) in k.hit:
            for dr, dc in ((0, 1), (1, 0)):
                if (r + dr, c + dc) not in k.hit:
                    continue

                a, b = r, c
                while (a - dr, b - dc) in k.hit:
                    a, b = a - dr, b - dc

                x, y = r, c
                while (x + dr, y + dc) in k.hit:
                    x, y = x + dr, y + dc

                for p in ((a - dr, b - dc), (x + dr, y + dc)):
                    if inside(p) and not k.tried(p):
                        ext.append(p)
        return ext

    def choose(self):
        k = self.k
        if k.hit:
            options = self.line_extensions() or self.adjacent_to_hits()
            if options:
                return self.rng.choice(options)

        step = min(k.remaining)
        parity = [p for p in k.untried() if (p[0] + p[1]) % step == 0]
        return self.rng.choice(parity or k.untried())


class ExpertAI(AI):
    def choose(self):
        return best_cell(self.k, rng=self.rng)


class Completion:
    """Lightweight stand-in for a Board used during Monte Carlo rollouts.

    Exposes exactly what _greedy_shots needs, but with O(1) ship lookups
    and no per-cell Board plumbing.
    """
    __slots__ = ("ship_cells", "true_cells", "cell_to_ship", "num_ships")

    def __init__(self, ship_cells: ShipCells) -> None:
        self.ship_cells = ship_cells
        true_cells = set()
        cell_to_ship = {}
        for cells in ship_cells.values():
            cells_list = list(cells)
            for p in cells_list:
                true_cells.add(p)
                cell_to_ship[p] = cells_list
        self.true_cells = true_cells
        self.cell_to_ship = cell_to_ship
        self.num_ships = len(ship_cells)

    @property
    def total_cells(self) -> int:
        return len(self.true_cells)


def random_completion(k, rng):
    blocked = k.miss | k.sunk
    hit = k.hit
    remaining = k.remaining

    for _ in range(60):
        ship_cells = {}
        occupied = set()
        ok = True

        for length in remaining:
            placed = False
            for _ in range(100):
                horiz = rng.random() < 0.5
                r = rng.randrange(SIZE)
                c = rng.randrange(SIZE)
                cells = line_cells(r, c, length, horiz)
                if cells is None:
                    continue
                cells_set = set(cells)
                if not cells_set.isdisjoint(occupied):
                    continue
                if not cells_set.isdisjoint(blocked):
                    continue
                name = "tmp%d_%d" % (length, rng.randrange(10 ** 6))
                ship_cells[name] = cells
                occupied |= cells_set
                placed = True
                break
            if not placed:
                ok = False
                break

        if not ok:
            continue
        if not hit <= occupied:
            continue

        return Completion(ship_cells)

    return None


def _greedy_shots(truth, k, first, rng, cap=100):
    kk = Knowledge()
    kk.remaining = list(k.remaining)
    kk.miss = set(k.miss)
    kk.hit = set(k.hit)
    kk.sunk = set(k.sunk)
    kk._hit_time = dict(k._hit_time)
    kk._sinkings = list(k._sinkings)
    kk._shots = k._shots

    true_cells = truth.true_cells
    cell_to_ship = truth.cell_to_ship
    num_ships = truth.num_ships
    sunk_count = 0

    shots = 0
    pending_first = True

    for _ in range(cap):
        if pending_first:
            pos, pending_first = first, False
        else:
            if not kk.untried():
                return shots
            pos = best_cell(kk, rng=rng)

        if pos in kk.miss or pos in kk._hit_time:
            continue

        shots += 1

        if pos in true_cells:
            cells = cell_to_ship[pos]
            all_hit = True
            for q in cells:
                if q != pos and q not in kk._hit_time:
                    all_hit = False
                    break
            sunk_len = len(cells) if all_hit else None
            kk.record(pos, True, sunk_len)
            if sunk_len is not None:
                sunk_count += 1
                if sunk_count >= num_ships:
                    return shots
        else:
            kk.record(pos, False, None)

    return cap


def _mc_pick(cands, means, ses, expert_pick, z=1.0):
    order = sorted(range(len(cands)), key=lambda i: means[i])
    w, runner = order[0], order[1]

    diff = means[runner] - means[w]
    se = math.sqrt(ses[w] ** 2 + ses[runner] ** 2)

    if diff > z * se:
        return cands[w]
    return expert_pick


class NightmareAI(AI):
    def __init__(self, rng=None, samples=60, candidates=12, z=2.0):
        super().__init__(rng=rng)
        self.samples = samples
        self.candidates = candidates
        self.z = z

    def choose(self):
        cands = [p for (p, _, _) in top_candidates(self.k, n=self.candidates, rng=self.rng)]
        if len(cands) <= 1:
            return best_cell(self.k, rng=self.rng)

        boards = []
        for _ in range(self.samples):
            sc = random_completion(self.k, self.rng)
            if sc is not None:
                boards.append(sc)

        if not boards:
            return best_cell(self.k, rng=self.rng)

        base = self.rng.randrange(2 ** 30)

        means, ses = [], []
        for idx, pos in enumerate(cands):
            crng = random.Random(base * 7919 + idx)
            tots = [float(_greedy_shots(sc, self.k, pos, crng)) for sc in boards]
            m = sum(tots) / len(tots)
            var = sum((t - m) ** 2 for t in tots) / len(tots)
            means.append(m)
            ses.append(math.sqrt(var / len(tots)) if tots else 0.0)

        expert_pick = best_cell(self.k, rng=self.rng)
        return _mc_pick(cands, means, ses, expert_pick, z=self.z)




LEVELS = [
    ("Easy", EasyAI, "Fires at random squares."),
    ("Medium", MediumAI, "Random search, then hunts around any hit."),
    ("Hard", HardAI, "Checkerboard search, follows lines of hits."),
    ("Expert", ExpertAI, "Maps every possible ship position, shoots the likeliest square."),
    ("Nightmare", NightmareAI, "Monte Carlo rollouts, picks fewest expected shots."),
]


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------

GAP = "      "


def own_char(board, r, c, last=None):
    ship = board.cells[r][c]
    shot = (r, c) in board.shots

    if ship and shot:
        if vis("damage_fire") and can_animate() and not board.is_sunk(ship):
            frame = int(time.time() * 4) % 2
            ch = paint("!", "red", "bold") if frame else paint("*", "yellow", "bold")
            if last == (r, c):
                ch = _highlight(ch)
            return ch
        ch = paint("X", "red", "bold")
    elif ship:
        ch = paint("S", "cyan", "bold")
    elif shot:
        ch = paint("o", "white")
    else:
        ch = water_char(r, c)

    if last == (r, c):
        ch = _highlight(ch)

    return ch


def track_char(enemy, r, c, reveal, last=None, reveal_cells=None):
    ship = enemy.cells[r][c]

    if reveal_cells is not None and (r, c) in reveal_cells:
        ch = paint("@", "cyan", "bold")
    elif (r, c) in enemy.shots:
        if not ship:
            ch = paint("o", "white")
        elif not enemy.is_sunk(ship) and vis("damage_fire") and can_animate():
            frame = int(time.time() * 4) % 2
            ch = paint("!", "red", "bold") if frame else paint("*", "yellow", "bold")
            if last == (r, c):
                ch = _highlight(ch)
            return ch
        else:
            ch = paint("#", "green", "bold") if enemy.is_sunk(ship) else paint("X", "yellow", "bold")
    elif reveal and ship:
        ch = paint("S", "cyan")
    else:
        ch = paint(".", "grey")

    if last == (r, c):
        ch = _highlight(ch)

    return ch


def _board_header():
    return "    " + " ".join(COLS)


def _own_row(board, r, last_ai=None):
    return "%2d  " % (r + 1) + " ".join(own_char(board, r, c, last=last_ai) for c in range(SIZE))


def _track_row(enemy, r, reveal=False, last_player=None, cursor=None, reveal_cells=None):
    cells = []
    for c in range(SIZE):
        ch = track_char(enemy, r, c, reveal, last=last_player, reveal_cells=reveal_cells)
        if cursor == (r, c):
            ch = cursor_reverse(ch)
        cells.append(ch)
    return "%2d  " % (r + 1) + " ".join(cells)


def _own_row_with_ghost(board, r, ghost, ghost_valid, ghost_cursor):
    cells = []
    for c in range(SIZE):
        if ghost_cursor == (r, c):
            ch = paint("@", "green", "bold") if ghost_valid else paint("@", "red", "bold")
        elif (r, c) in ghost:
            ch = paint("+", "green", "bold") if ghost_valid else paint("+", "red", "bold")
        else:
            ch = own_char(board, r, c)
        cells.append(ch)
    return "%2d  " % (r + 1) + " ".join(cells)


def render_boards(player, enemy, reveal=False, last_player=None, last_ai=None, cursor=None, reveal_cells=None):
    left_rows = [_board_header()] + [_own_row(player, r, last_ai) for r in range(SIZE)]
    right_rows = [_board_header()] + [_track_row(enemy, r, reveal, last_player, cursor, reveal_cells) for r in range(SIZE)]

    left_box = boxed_panel(paint("YOUR FLEET", "bold"), left_rows)
    right_box = boxed_panel(paint("ENEMY WATERS", "bold"), right_rows)

    return "\n".join("  " + line for line in side_by_side(left_box, right_box, gap="   "))


def render_own(board, cursor=None, ghost=None, ghost_valid=True, ghost_cursor=None):
    ghost = ghost or set()
    rows = [_board_header()]
    for r in range(SIZE):
        if ghost or ghost_cursor:
            row = _own_row_with_ghost(board, r, ghost, ghost_valid, ghost_cursor)
            if cursor and cursor[0] == r:
                cells = []
                for c in range(SIZE):
                    ch = own_char(board, r, c)
                    if cursor == (r, c):
                        ch = cursor_reverse(ch)
                    cells.append(ch)
                row_parts = row.split("  ", 1)
                row = row_parts[0] + "  " + " ".join(cells)
        else:
            row = _own_row(board, r)
            if cursor and cursor[0] == r:
                cells = []
                for c in range(SIZE):
                    ch = own_char(board, r, c)
                    if cursor == (r, c):
                        ch = cursor_reverse(ch)
                    cells.append(ch)
                row = "%2d  " % (r + 1) + " ".join(cells)
        rows.append(row)

    return "\n".join("  " + line for line in boxed_panel(paint("YOUR FLEET", "bold"), rows))


def _strip_prefix(lines):
    return [l[2:] if l.startswith("  ") else l for l in lines]


def legend():
    return ("Legend: %s water  %s your ship  %s hit (yellow=enemy, red=own)  %s miss  %s sunk  %s not fired at"
            % (paint("~", "blue"), paint("S", "cyan", "bold"), paint("X", "yellow", "bold"),
               paint("o", "white"), paint("#", "green", "bold"), paint(".", "grey")))


def fleet_text(names):
    return ", ".join("%s(%d)" % (n, SHIP_LEN[n]) for n in names) or "none"


def fleet_damage_text(board):
    parts = []

    for name, length in FLEET:
        if name not in board.ship_cells:
            continue

        hits = sum(1 for p in board.ship_cells[name] if p in board.shots and board.shots[p])

        if hits == length:
            bar = paint("#" * length, "red", "bold")

            if vis("fleet_status"):
                parts.append("%s %s %s" % (name, bar, paint("LOST", "red", "bold")))
            else:
                parts.append("%s %s SUNK" % (name, bar))
        else:
            if hits == length - 1:
                bar = paint("#" * hits, "red", "bold") + paint("-" * (length - hits), "grey")
            else:
                bar = paint("#" * hits, "yellow") + paint("-" * (length - hits), "grey")

            if vis("fleet_status"):
                if hits == 0:
                    status = paint("OK", "green")
                elif hits == length - 1:
                    status = paint("CRITICAL", "red", "bold")
                else:
                    status = paint("DAMAGED", "yellow")

                parts.append("%s %s %d/%d %s" % (name, bar, hits, length, status))
            else:
                parts.append("%s %s %d/%d" % (name, bar, hits, length))

    return "  ".join(parts) or "none"


# ----------------------------------------------------------------------------
# Interactive fleet placement (shared by solo, campaign, hotseat)
# ----------------------------------------------------------------------------

def interactive_place_fleet(board):
    """Cursor-based ship placement. Returns final cursor (r,c) on success, None on abort."""
    placed = 0
    cursor = (0, 0)
    horiz = True
    msg = ""

    with KeyReader() as kr:
        while True:
            clear()
            if placed < len(FLEET):
                name, length = FLEET[placed]
                ghost, err = board.check_placement(length, cursor[0], cursor[1], horiz)
                valid = (ghost is not None)
                ghost_set = set(ghost) if ghost else set()
            else:
                name, length = None, None
                valid = True
                ghost_set = set()

            board_lines = _strip_prefix(
                render_own(board, ghost=ghost_set, ghost_valid=valid,
                           ghost_cursor=cursor).split("\n"))
            fleet_lines = fleet_panel(board, current_name=name if name else None)
            for line in side_by_side(board_lines, fleet_lines, gap="  "):
                print("  " + line)

            print(legend())
            print()
            if name:
                orient = "HORIZONTAL" if horiz else "VERTICAL"
                print("  " + paint("Placing:", "bold") + " %s (%d)   " % (name, length) +
                      paint("Cursor:", "bold") + " %s   " % cell_name(cursor) +
                      paint("Dir:", "bold") + " %s" % orient)
            else:
                print("  " + paint("All ships placed.", "green", "bold") + "  Press Enter to continue.")
            print("  Ships placed: %d/%d" % (placed, len(FLEET)))
            if msg:
                print(msg)
            print()
            print("  " + paint("─" * 46, "grey"))
            print("  Arrows move · R rotate · Enter place · Z undo · Q quit")

            key = kr.get_key()
            msg = ""

            if key == "UP":
                cursor = (max(0, cursor[0] - 1), cursor[1])
            elif key == "DOWN":
                cursor = (min(SIZE - 1, cursor[0] + 1), cursor[1])
            elif key == "LEFT":
                cursor = (cursor[0], max(0, cursor[1] - 1))
            elif key == "RIGHT":
                cursor = (cursor[0], min(SIZE - 1, cursor[1] + 1))
            elif key in ("R", "r"):
                horiz = not horiz
            elif key in ("Z", "z", "BACKSPACE"):
                undone = board.undo()
                if undone:
                    placed = max(0, placed - 1)
                    msg = "  " + paint("Removed %s." % undone, "yellow")
                else:
                    msg = "  Nothing to undo."
            elif key == "ENTER":
                if placed == len(FLEET):
                    return cursor
                cells, err = board.check_placement(length, cursor[0], cursor[1], horiz)
                if err:
                    msg = "  " + paint(err, "red")
                    continue
                board.place(name, cells)
                placed += 1
            elif key in ("Q", "q", "ESC", "CTRL_C"):
                return None


# ----------------------------------------------------------------------------
# Guide text
# ----------------------------------------------------------------------------

HOW_TO_PLAY = """
HOW TO PLAY
1. Each side hides ships on a grid. Ship count and board size vary by setup.
2. Ships lie in straight lines, across or down. They never overlap.
3. Take turns firing at one square. You fire first.
4. Squares are named column letter + row number: A1 (top-left).
5. You are told HIT, MISS or SUNK. A ship sinks when all its squares are hit.
   First to sink the other fleet wins.

SYMBOLS
  ~ water     S your ship     X hit     o miss     # sunk enemy ship     . not fired

CONTROLS (interactive mode)
  Arrows / WASD    move the cursor
  Enter or Space   fire at the cursor
  A–J              jump to that column
  1–9, 0           jump to that row (0 = row 10)
  ?                toggle hint suggestions
  /                toggle density map
  W                save game
  Q or Esc         abandon the game
  You can still type a cell (e.g. B7) and press Enter.

PLACING SHIPS (interactive mode)
  Arrows           move the placement cursor
  R                rotate (horizontal / vertical)
  Enter            place the ship
  Z / Backspace    undo the last ship
  Q or Esc         abandon setup
"""

SHOT_HELP = """
Interactive controls:
  Arrows / WASD    move cursor
  Enter / Space    fire at cursor
  A–J               jump to column
  1–9, 0            jump to row
  ?                toggle hint
  /                toggle density map
  W                save game
  Q / Esc          abandon
Or type a cell like B7 and press Enter.
"""

PLACE_HELP = """
Interactive controls:
  Arrows            move placement cursor
  R                 rotate
  Enter             place ship
  Z / Backspace     undo last ship
  Q / Esc           abandon setup
Or type a start cell and direction like A1 H.
"""


# ----------------------------------------------------------------------------
# Shot messages & log
# ----------------------------------------------------------------------------

def shot_msg_player(pos, hit, ship, sunk):
    cell = cell_name(pos)
    if sunk:
        return paint(">>> SUNK! You destroyed the enemy %s (%d) with %s!" % (ship, SHIP_LEN[ship], cell),
                     "green", "bold")
    if hit:
        return paint(">>> HIT at %s!" % cell, "yellow", "bold")
    return "You fired at %s: miss." % cell


def shot_msg_ai(pos, hit, ship, sunk):
    cell = cell_name(pos)
    if sunk:
        return paint("Enemy fires at %s: SUNK your %s!" % (cell, ship), "red", "bold")
    if hit:
        return paint("Enemy fires at %s: hit on your %s." % (cell, ship), "red")
    return "Enemy fires at %s: miss." % cell


def print_log(notes):
    if not notes:
        return
    print("  " + paint("── LOG " + "─" * 46, "grey"))
    for note in notes:
        print("    " + note)


# ----------------------------------------------------------------------------
# Shot review & coach
# ----------------------------------------------------------------------------

def show_shot_review(game):
    """Full shot-by-shot review with per-turn top-3 annotations."""
    clear()
    print()
    for line in big_banner("SHOT REVIEW", "cyan"):
        print("  " + line)
    print()

    history = getattr(game, "shot_history", [])
    if not history:
        print("  No shots recorded.")
        print()
        return

    for i, e in enumerate(history, 1):
        cell = cell_name(e["pos"])
        if e["sunk"]:
            desc = paint("SUNK %s" % (e["ship"] or "ship"), "green", "bold")
        elif e["hit"]:
            desc = paint("HIT", "yellow", "bold")
        else:
            desc = paint("miss", "white")
        suffix = ""
        if not e["coach_opt"] and e.get("top3"):
            top3 = ", ".join(cell_name(p) for p in e["top3"])
            suffix = "   " + paint("(top-3: %s)" % top3, "grey")
        print("  %3d.  %-5s  %s%s" % (i, cell, desc, suffix))

    print()
    print_coach_summary(history)


def print_coach_summary(history):
    total = len(history)
    if not total:
        return
    hits = sum(1 for e in history if e["hit"])
    opt = sum(1 for e in history if e["coach_opt"])
    off = total - opt

    print("  " + paint("── COACH " + "─" * 46, "grey"))
    print("    Shots: %d   Hits: %d   Accuracy: %d%%"
          % (total, hits, 100 * hits // total))
    print("    Expert top-3 picks: %d/%d (%d%%)" % (opt, total, 100 * opt // total))

    if off:
        print()
        print("    Shots outside the expert top-3:")
        for i, e in enumerate(history, 1):
            if not e["coach_opt"]:
                top3 = ", ".join(cell_name(p) for p in e["top3"])
                desc = "HIT" if e["hit"] else "miss"
                print("      Turn %2d:  %-5s  %-5s   top-3: %s"
                      % (i, cell_name(e["pos"]), desc, top3))
    else:
        print()
        print("    " + paint("Flawless! Every shot was in the expert top-3.", "green", "bold"))


# ----------------------------------------------------------------------------
# Game
# ----------------------------------------------------------------------------

class Game:
    # Logical order: __init__ -> setup -> display -> shooting -> history -> main loop -> finish
    # Public methods first, then properties, then private (_-prefixed) helpers.
    def __init__(self, level_name: str, ai_cls: Any, mode: str = "single",
                 contrarian: bool = False, reuse_fleet: Optional[ShipCells] = None) -> None:
        self.level = level_name
        self.ai = ai_cls()
        self.mode = mode
        self.contrarian = contrarian

        self.player = Board()
        if reuse_fleet is not None:
            for name, cells in reuse_fleet.items():
                self.player.place(name, list(cells))

        self.enemy = Board()
        if contrarian:
            self.enemy.place_contrarian(FLEET)
        else:
            self.enemy.place_randomly(FLEET)

        self.pk = Knowledge()
        self.turn = 1
        self.stats: Dict[str, int] = {"shots": 0, "hits": 0, "hints": 0, "ai_shots": 0, "ai_hits": 0,
                      "coach_opt": 0, "coach_total": 0}
        self._stats_obj = GameStats()  # typed companion; self.stats kept for compatibility
        self.cursor = None
        self.shot_history = []

    # -- Setup ----------------------------------------------------------------

    def setup(self):
        if not supports_cursor_ui():
            return self._setup_typed()

        choice = select_menu(
            paint("FLEET SETUP", "bold"),
            ["Place my ships by hand", "Random layout"],
            start_idx=_MENU_STATE.last_setup_choice,
        )
        _MENU_STATE.last_setup_choice = choice

        if choice == 1:
            self.player.place_randomly(FLEET)
            clear()
            print(render_own(self.player))
            print()
            print("Random layout shown above.")
            with KeyReader() as kr:
                while True:
                    print("  Enter = start · R = reroll · Q = abandon")
                    key = kr.get_key()
                    if key == "ENTER":
                        return True
                    if key in ("R", "r"):
                        while self.player.undo():
                            pass
                        self.player.place_randomly(FLEET)
                        clear()
                        print(render_own(self.player))
                        print()
                        continue
                    if key in ("Q", "q", "ESC", "CTRL_C"):
                        return False

        return self._setup_cursor()

    def _setup_typed(self):
        print("\nFLEET SETUP")
        print("  1) Place my ships by hand")
        print("  2) Random layout")

        if pick("Choose 1 or 2 > ", ["1", "2"]) == "2":
            self.player.place_randomly(FLEET)

        board = self.player
        while True:
            print("\n" + render_own(board))
            n = len(board.order)

            if n == len(FLEET):
                raw = ask("Fleet ready. Enter = start | undo | random = new layout | help > ").lower()
                if raw == "":
                    return True
            else:
                name, length = FLEET[n]
                raw = ask("[%d/%d] Place %s (%d squares). Example: A1 H  (or help) > "
                          % (n + 1, len(FLEET), name, length)).lower()

            if raw in ("quit", "q", "exit"):
                if confirm("  Abandon setup?"):
                    return False
                continue

            if raw in ("help", "h", "?"):
                print(PLACE_HELP)
            elif raw == "undo":
                undone = board.undo()
                print("  Removed %s." % undone if undone else "  Nothing to undo.")
            elif raw in ("random", "auto", "r"):
                while board.undo():
                    pass
                board.place_randomly(FLEET)
            elif raw == "":
                continue
            elif n < len(FLEET):
                parsed = parse_placement(raw)
                if not parsed:
                    print("  Can't read that. Use START DIRECTION, e.g. A1 H (across) or A1 V (down).")
                    continue
                (r, c), horizontal = parsed
                cells, err = board.check_placement(length, r, c, horizontal)
                if err:
                    print("  Can't place %s at %s %s: %s." %
                          (name, cell_name((r, c)), "H" if horizontal else "V", err))
                else:
                    board.place(name, cells)
            else:
                print("  Press Enter to start, or type undo / random.")

    def _setup_cursor(self):
        result = interactive_place_fleet(self.player)
        if result is None:
            return False
        self.cursor = result
        return True

    # -- Display --------------------------------------------------------------

    def status_lines(self) -> List[str]:
        s = self.stats
        acc = "%d%%" % (100 * s["hits"] // s["shots"]) if s["shots"] else "-"
        return [
            "%s %d  ·  %s  ·  You: %d shots, %d hits (%s)"
            % (paint("Turn", "bold"), self.turn, self.level, s["shots"], s["hits"], acc),
            "%s  %s" % (paint("Your fleet:  ", "bold"), fleet_damage_text(self.player)),
            "%s %s" % (paint("Enemy afloat:", "bold"), fleet_text(self.enemy.afloat())),
        ]

    def show(self, notes, cursor=None, extras=None):
        clear()
        print(render_boards(self.player, self.enemy,
                            last_player=getattr(self, "last_player", None),
                            last_ai=getattr(self, "last_ai", None),
                            cursor=cursor))
        print()
        for line in boxed_panel(paint("STATUS", "bold"), self.status_lines()):
            print("  " + line)
        if extras:
            print()
            for line in extras:
                print("  " + line)
        print()
        print_log(notes)

    # -- Properties (computed views; kept after public display methods) ---------
    @property
    def shots_fired(self) -> int:
        return int(self.stats.get("shots", 0))

    @property
    def is_single_player(self) -> bool:
        return self.mode != "salvo"

    @property
    def stats_obj(self) -> GameStats:
        """Typed snapshot of the stats dict."""
        return GameStats.from_dict(self.stats)

    def _first_untried(self):  # type: ignore[no-untyped-def]
        for r in range(SIZE):
            for c in range(SIZE):
                if (r, c) not in self.enemy.shots:
                    return (r, c)
        return (0, 0)

    def _cursor_shot_render(self, cursor, notes, extra="", hint=None, density=None):  # type: ignore[no-untyped-def]
        extras = []
        if extra:
            extras.append(extra)
        if hint is not None:
            extras.append("")
            extras.extend(hint.split("\n"))
        if density is not None:
            extras.append("")
            extras.extend(density.split("\n"))
        self.show(notes, cursor=cursor, extras=extras)
        print()
        print("  " + paint("─" * 60, "grey"))
        print("  Arrows move · Enter fire · A–J col · 1–0 row · ? hint · / map · W save · Q quit")

    # -- Shooting -------------------------------------------------------------

    def get_shot(self, notes):
        burst_banner("YOUR TURN", ("cyan", "bold"), 0.28)

        if not supports_cursor_ui():
            return self._get_shot_typed(notes)

        return self._get_shot_cursor(notes)

    def _get_shot_typed(self, notes):
        while True:
            raw0 = ask("Your shot (e.g. B7 | hint | help | quit) > ")
            raw = raw0.lower()

            if raw in ("help", "h", "?"):
                print(SHOT_HELP)
            elif raw == "hint":
                self.stats["hints"] += 1
                print("  %s" % hint_text(self.pk, rng=getattr(self.ai, "rng", random)).replace("\n", "\n  "))
            elif raw == "map":
                print(render_density(self.pk))
            elif raw == "board":
                self.show(notes)
            elif raw.startswith("save "):
                fname = raw0[5:].strip()
                if not fname:
                    print("  Give a filename: save NAME.")
                else:
                    try:
                        save_game(self, fname)
                        print("  Saved to %s." % fname)
                    except OSError as e:
                        print("  Could not save: %s." % e)
            elif raw in ("quit", "q", "exit"):
                if confirm("  Abandon this game?"):
                    return None
            else:
                pos = parse_cell(raw)
                if pos is None:
                    print("  %s." % shot_error(raw))
                elif pos in self.enemy.shots:
                    print("  You already fired at %s. Pick another square." % cell_name(pos))
                else:
                    return pos

    def _get_shot_cursor(self, notes):
        while True:
            try:
                with KeyReader() as kr:
                    action, value = self._cursor_shot_loop(kr, notes)
            except Quit:
                return None

            if action == "FIRE":
                return value
            if action == "QUIT":
                if confirm("  Abandon this game?"):
                    return None
                continue
            if action == "SAVE":
                fname = ask("  Save as > ").strip()
                if fname:
                    try:
                        save_game(self, fname)
                        print("  Saved to %s." % fname)
                    except OSError as e:
                        print("  Could not save: %s." % e)
                continue

    def _cursor_shot_loop(self, kr, notes):
        cursor = self.cursor if self.cursor is not None else self._first_untried()
        hint = None
        density = None
        extra = ""

        while True:
            self._cursor_shot_render(cursor, notes, extra, hint, density)
            key = kr.get_key()
            extra = ""

            if key == "UP":
                cursor = (max(0, cursor[0] - 1), cursor[1])
            elif key == "DOWN":
                cursor = (min(SIZE - 1, cursor[0] + 1), cursor[1])
            elif key == "LEFT":
                cursor = (cursor[0], max(0, cursor[1] - 1))
            elif key == "RIGHT":
                cursor = (cursor[0], min(SIZE - 1, cursor[1] + 1))
            elif key in ("ENTER", " "):
                if cursor in self.enemy.shots:
                    extra = paint("  Already fired at %s." % cell_name(cursor), "yellow")
                    continue
                self.cursor = cursor
                return ("FIRE", cursor)
            elif key in ("Q", "q", "ESC"):
                return ("QUIT", None)
            elif key == "CTRL_C":
                raise Quit
            elif key == "?":
                self.stats["hints"] += 1
                hint = None if hint is not None else hint_text(
                    self.pk, rng=getattr(self.ai, "rng", random))
            elif key == "/":
                density = None if density is not None else render_density(self.pk)
            elif key in ("W", "w"):
                return ("SAVE", None)
            elif key and len(key) == 1:
                up = key.upper()
                if up in COLS:
                    cursor = (cursor[0], COLS.index(up))
                elif key in "123456789":
                    cursor = (int(key) - 1, cursor[1])
                elif key == "0" and SIZE >= 10:
                    cursor = (9, cursor[1])

    # -- Shot-history helper --------------------------------------------------

    def _record_shot_history(self, pos, hit, ship, sunk, top3, coach_opt):
        self._stats_obj.streak = self._stats_obj.streak + 1 if hit else 0
        self.stats["streak"] = self._stats_obj.streak
        self.shot_history.append({
            "pos": pos,
            "hit": hit,
            "sunk": sunk,
            "ship": ship if sunk else None,
            "top3": top3,
            "coach_opt": coach_opt,
        })

    # -- Turn helpers (split from God-method run) --------------------------------
    def _take_player_salvo(self, notes: List[str]) -> Optional[List[Cell]]:
        """Collect one salvo of shots; returns None if player quits."""
        n = salvo_size(self.player)
        turn_shots: List[Cell] = []
        while len(turn_shots) < n:
            pos = self.get_shot(notes)
            if pos is None:
                return None
            if pos in turn_shots:
                continue
            turn_shots.append(pos)
        return turn_shots

    def _enemy_salvo_response(self) -> List[tuple]:
        ai_moves: List[tuple] = []
        for _ in range(salvo_size(self.enemy)):
            apos = self.ai.choose()
            ahit, aship, asunk = self.player.fire(apos)
            self.ai.record(apos, ahit, SHIP_LEN[aship] if asunk else None)
            self.stats["ai_shots"] += 1
            self.stats["ai_hits"] += ahit
            self.last_ai = apos
            ai_moves.append((apos, ahit, aship, asunk))
            if self.player.all_sunk():
                break
        return ai_moves

    def _enemy_single_response(self) -> tuple:
        apos = self.ai.choose()
        ahit, aship, asunk = self.player.fire(apos)
        self.ai.record(apos, ahit, SHIP_LEN[aship] if asunk else None)
        self.stats["ai_shots"] += 1
        self.stats["ai_hits"] += ahit
        self.last_ai = apos
        return apos, ahit, aship, asunk

    # -- Main loop ------------------------------------------------------------

    def run(self) -> str:
        self.last_player = None
        self.last_ai = None
        _reset_burst_state()

        if self.player.order and len(self.player.order) == len(FLEET):
            notes = ["Resumed. Your move."]
        elif not self.setup():
            return "abandoned"
        else:
            notes = ["Your move. You fire first."]

        while True:
            if not supports_cursor_ui():
                self.show(notes)

            if self.mode == "salvo":
                turn_shots = self._take_player_salvo(notes)
                if turn_shots is None:
                    self.finish_abandoned()
                    return "abandoned"
                n = len(turn_shots)

                top3_list = [top_candidates(self.pk, n=3) for _ in turn_shots]
                opts = []
                for pos, top in zip(turn_shots, top3_list):
                    top_pts = [p for (p, _, _) in top]
                    opts.append(pos in top_pts)
                    self.stats["coach_opt"] += (pos in top_pts)
                    self.stats["coach_total"] += 1
                    self.stats["shots"] += 1

                results = apply_salvo(self.enemy, self.pk, turn_shots)

                for pos, (hit, ship, sunk), top, opt in zip(turn_shots, results, top3_list, opts):
                    burst_shot(pos, hit, ship, sunk, opp=False, level=self.level); show_sunk_reveal(self.player, self.enemy, ship, pos) if sunk else None
                    self._record_shot_history(pos, hit, ship, sunk,
                                              [p for (p, _, _) in top], opt)

                self.last_player = turn_shots[-1]
                notes = [shot_msg_player(pos, hit, ship, sunk)
                         for pos, (hit, ship, sunk) in zip(turn_shots, results)]
                self.stats["hits"] += sum(1 for hit, _, _ in results)

                if self.enemy.all_sunk():
                    self.finish(True, notes)
                    return "win"

                # Redraw now so the player's shot marks are visible before
                # the AI starts thinking (matters most for slow AIs).
                self.show(notes, cursor=self.cursor)

                with Spinner("Enemy is calculating"):
                    ai_moves = self._enemy_salvo_response()

                for apos, ahit, aship, asunk in ai_moves:
                    burst_shot(apos, ahit, aship, asunk, opp=True, level=self.level)
                    notes.append(shot_msg_ai(apos, ahit, aship, asunk))

                if self.player.all_sunk():
                    self.finish(False, notes)
                    return "loss"

                self.turn += 1
                continue

            pos = self.get_shot(notes)
            if pos is None:
                self.finish_abandoned()
                return "abandoned"

            top3 = [p for (p, _, _) in top_candidates(self.pk, n=3)]
            coach_opt = pos in top3

            hit, ship, sunk = self.enemy.fire(pos)
            burst_shot(pos, hit, ship, sunk, opp=False, level=self.level)

            self.stats["coach_opt"] += coach_opt; show_sunk_reveal(self.player, self.enemy, ship, pos) if sunk else None
            self.stats["coach_total"] += 1
            self.pk.record(pos, hit, SHIP_LEN[ship] if sunk else None)
            self.stats["shots"] += 1
            self.stats["hits"] += hit
            self.last_player = pos
            notes = [shot_msg_player(pos, hit, ship, sunk)]
            self._record_shot_history(pos, hit, ship, sunk, top3, coach_opt)

            if self.enemy.all_sunk():
                self.finish(True, notes)
                return "win"

            # Redraw now so the player's shot mark is visible before
            # the AI starts thinking (matters most for slow AIs).
            self.show(notes, cursor=self.cursor)

            with Spinner("Enemy is calculating"):
                apos, ahit, aship, asunk = self._enemy_single_response()

            burst_shot(apos, ahit, aship, asunk, opp=True, level=self.level)
            notes.append(shot_msg_ai(apos, ahit, aship, asunk))

            if self.player.all_sunk():
                self.finish(False, notes)
                return "loss"

            self.turn += 1

    def finish_abandoned(self):
        clear()
        print(render_boards(self.player, self.enemy, reveal=True,
                            last_player=getattr(self, "last_player", None),
                            last_ai=getattr(self, "last_ai", None)))
        print()
        print("  " + paint("Game abandoned. Enemy fleet revealed above.", "yellow"))
        print()
        for line in boxed_panel(paint("STATUS", "bold"), self.status_lines()):
            print("  " + line)

    def rebuild_knowledge(self):
        self.pk = Knowledge.from_board(self.enemy)

    def finish(self, won, notes):
        clear()
        print(render_boards(self.player, self.enemy, reveal=not won))
        finish_cinematic(won)
        print()

        for line in big_banner("V I C T O R Y" if won else "D E F E A T",
                               "green" if won else "red"):
            print("  " + line)
        print()

        for note in notes:
            print("    " + note)
        print()

        s = self.stats
        if won:
            par = expert_par({n: list(c) for n, c in self.enemy.ship_cells.items()})
            print("  " + paint(par_line(s["shots"], par), "cyan"))

        acc = "%d%%" % (100 * s["hits"] // s["shots"]) if s["shots"] else "-"
        summary = [
            "Turns: %d" % self.turn,
            "Your shots:   %d  (%d hits, %s)" % (s["shots"], s["hits"], acc),
            "Enemy shots:  %d  (%d hits)" % (s["ai_shots"], s["ai_hits"]),
            "Ships afloat: %d/%d" % (len(self.player.afloat()), len(FLEET)),
            "Hints used:   %d" % s["hints"],
            coach_line(s["coach_opt"], s["coach_total"]),
            "War: %s" % war_sparkline(self.shot_history),
            mvp_line(self),
        ]
        print()
        for line in boxed_panel(paint("SUMMARY", "bold"), summary):
            print("  " + line)


# ----------------------------------------------------------------------------
# Campaign
# ----------------------------------------------------------------------------

class CampaignGame:
    # Logical order: __init__ -> public run -> private _between_missions/_show_summary
    """Runs through every difficulty in sequence. The same player fleet is
    redeployed for each mission. A single loss ends the campaign."""

    def __init__(self, mode="single", contrarian=False):
        self.mode = mode
        self.contrarian = contrarian
        self.missions = [name for name, _, _ in LEVELS]
        self.player_fleet = None
        self.results = []
        self.total_shots = 0
        self.total_hits = 0
        self.total_ai_shots = 0
        self.total_ai_hits = 0

    def run(self):
        for i, level_name in enumerate(self.missions):
            cls = dict((n, c) for n, c, _ in LEVELS)[level_name]
            game = Game(level_name, cls, mode=self.mode,
                        contrarian=self.contrarian,
                        reuse_fleet=self.player_fleet)
            result = game.run()

            if result == "abandoned":
                self._show_summary("A B A N D O N E D", "yellow", lost_at=None)
                return "abandoned"

            if self.player_fleet is None and len(game.player.order) == len(FLEET):
                self.player_fleet = {n: list(c) for n, c in game.player.ship_cells.items()}

            won = (result == "win")
            self.results.append((level_name, won, game.stats["shots"], game.stats["hits"]))
            self.total_shots += game.stats["shots"]
            self.total_hits += game.stats["hits"]
            self.total_ai_shots += game.stats["ai_shots"]
            self.total_ai_hits += game.stats["ai_hits"]

            if not won:
                self._show_summary("C A M P A I G N   L O S T", "red", lost_at=level_name)
                return "loss"

            if i < len(self.missions) - 1:
                if not self._between_missions(i):
                    self._show_summary("A B A N D O N E D", "yellow", lost_at=None)
                    return "abandoned"

        self._show_summary("C A M P A I G N   C O M P L E T E", "green", lost_at=None)
        return "win"

    def _between_missions(self, done_idx):
        clear()
        print()
        for line in big_banner("MISSION COMPLETE", "green"):
            print("  " + line)
        print()
        print("  You defeated %s." % self.missions[done_idx])
        if done_idx + 1 < len(self.missions):
            print("  Next: %s" % self.missions[done_idx + 1])
        print()
        print("  Campaign so far:")
        for name, won, shots, hits in self.results:
            acc = "%d%%" % (100 * hits // shots) if shots else "-"
            mark = paint("WIN ", "green", "bold") if won else paint("LOSS", "red", "bold")
            print("    %s  %-7s  %d shots (%s)" % (mark, name, shots, acc))
        print()

        if supports_cursor_ui():
            with KeyReader() as kr:
                print("  Press Enter for next mission, or Q to end campaign...")
                while True:
                    k = kr.get_key()
                    if k == "ENTER":
                        return True
                    if k in ("Q", "q", "ESC", "CTRL_C"):
                        return False
        return confirm("Continue?")

    def _show_summary(self, title, color, lost_at):
        clear()
        print()
        for line in big_banner(title, color):
            print("  " + line)
        print()

        print("  Missions:")
        for name, won, shots, hits in self.results:
            acc = "%d%%" % (100 * hits // shots) if shots else "-"
            mark = paint("WIN ", "green", "bold") if won else paint("LOSS", "red", "bold")
            print("    %s  %-7s  %d shots (%s)" % (mark, name, shots, acc))

        if lost_at:
            print()
            print("  Campaign stopped at %s." % lost_at)

        print()
        acc = "%d%%" % (100 * self.total_hits // self.total_shots) if self.total_shots else "-"
        summary = [
            "Missions won:   %d/%d" % (sum(1 for r in self.results if r[1]), len(self.missions)),
            "Total shots:    %d" % self.total_shots,
            "Total hits:     %d  (%s)" % (self.total_hits, acc),
            "Enemy shots:    %d" % self.total_ai_shots,
            "Enemy hits:     %d" % self.total_ai_hits,
        ]
        print()
        for line in boxed_panel(paint("CAMPAIGN SUMMARY", "bold"), summary):
            print("  " + line)


# ----------------------------------------------------------------------------
# Hotseat
# ----------------------------------------------------------------------------

def next_seat(i):
    return 1 - i


class HotseatGame:
    # Logical order: __init__ -> public fire_shells/handoff/setup/run -> private _first_untried/_get_shot_*
    def __init__(self):
        self.boards = [Board(), Board()]
        self.knows = [Knowledge(), Knowledge()]
        self.stats = [{"shots": 0, "hits": 0}, {"shots": 0, "hits": 0}]
        self.cursors = [None, None]

    def fire_shells(self, seat, positions):
        foe, know = self.boards[next_seat(seat)], self.knows[seat]
        out = []
        for pos in positions:
            hit, ship, sunk = foe.fire(pos)
            know.record(pos, hit, SHIP_LEN[ship] if sunk else None)
            self.stats[seat]["shots"] += 1
            self.stats[seat]["hits"] += hit
            out.append((hit, ship, sunk))
        return out

    def handoff(self, seat):
        _reset_burst_state()
        clear()
        print()
        for line in big_banner("PLAYER %d" % (seat + 1), "cyan"):
            print("  " + line)
        print()
        print("  " + paint("Hand the terminal to Player %d. Opponent look away.", "bold") % (seat + 1))
        if supports_cursor_ui():
            with KeyReader() as kr:
                print("  Press Enter when ready...")
                while True:
                    k = kr.get_key()
                    if k == "ENTER":
                        return
                    if k in ("Q", "q", "ESC", "CTRL_C"):
                        raise Quit
        else:
            ask("  Press Enter when ready > ")

    def setup_board(self, seat):
        board = self.boards[seat]
        if not supports_cursor_ui():
            board.place_randomly(FLEET)
            return True

        choice = select_menu(
            paint("PLAYER %d — FLEET SETUP" % (seat + 1), "bold"),
            ["Place my ships by hand", "Random layout"],
            start_idx=_MENU_STATE.last_setup_choice,
        )
        _MENU_STATE.last_setup_choice = choice

        if choice == 1:
            board.place_randomly(FLEET)
        else:
            if interactive_place_fleet(board) is None:
                return False

        clear()
        print(render_own(board))
        print()
        if supports_cursor_ui():
            with KeyReader() as kr:
                print("  Player %d fleet ready. Press Enter to hand over." % (seat + 1))
                while True:
                    k = kr.get_key()
                    if k == "ENTER":
                        return True
                    if k in ("Q", "q", "ESC", "CTRL_C"):
                        return False
        else:
            ask("  Player %d fleet ready. Press Enter to continue > " % (seat + 1))
        return True

    def _first_untried(self, foe):
        for r in range(SIZE):
            for c in range(SIZE):
                if (r, c) not in foe.shots:
                    return (r, c)
        return (0, 0)

    def get_shot(self, seat):
        if not supports_cursor_ui():
            return self._get_shot_typed(seat)
        return self._get_shot_cursor(seat)

    def _get_shot_typed(self, seat):
        foe = self.boards[next_seat(seat)]
        while True:
            raw = ask("Player %d shot (e.g. B7 | quit) > " % (seat + 1)).lower()
            if raw in ("quit", "q", "exit"):
                if confirm("  Abandon this game?"):
                    return None
            else:
                pos = parse_cell(raw)
                if pos is None:
                    print("  %s." % shot_error(raw))
                elif pos in foe.shots:
                    print("  Already fired at %s. Pick another square." % cell_name(pos))
                else:
                    return pos

    def _get_shot_cursor(self, seat):
        foe = self.boards[next_seat(seat)]
        cursor = self.cursors[seat] if self.cursors[seat] is not None else self._first_untried(foe)
        extra = ""
        while True:
            clear()
            for line in big_banner("PLAYER %d" % (seat + 1), "cyan"):
                print("  " + line)
            print()

            left_rows = [_board_header()] + [_own_row(self.boards[seat], r) for r in range(SIZE)]
            right_rows = [_board_header()] + [_track_row(foe, r, False, cursor=cursor) for r in range(SIZE)]
            left_box = boxed_panel(paint("YOUR FLEET", "bold"), left_rows)
            right_box = boxed_panel(paint("ENEMY WATERS", "bold"), right_rows)
            for line in side_by_side(left_box, right_box, gap="   "):
                print("  " + line)

            print()
            print(legend())
            print()
            status = [
                "Player %d  ·  Shots: %d  ·  Hits: %d"
                % (seat + 1, self.stats[seat]["shots"], self.stats[seat]["hits"]),
                "Enemy afloat: " + fleet_text(foe.afloat()),
            ]
            for line in boxed_panel(paint("STATUS", "bold"), status):
                print("  " + line)
            if extra:
                print()
                print("  " + extra)
            print()
            print("  " + paint("─" * 60, "grey"))
            print("  Arrows move · Enter fire · A–J col · 1–0 row · Q quit")

            try:
                with KeyReader() as kr:
                    key = kr.get_key()
            except Quit:
                return None

            extra = ""
            if key == "UP":
                cursor = (max(0, cursor[0] - 1), cursor[1])
            elif key == "DOWN":
                cursor = (min(SIZE - 1, cursor[0] + 1), cursor[1])
            elif key == "LEFT":
                cursor = (cursor[0], max(0, cursor[1] - 1))
            elif key == "RIGHT":
                cursor = (cursor[0], min(SIZE - 1, cursor[1] + 1))
            elif key in ("ENTER", " "):
                if cursor in foe.shots:
                    extra = paint("Already fired at %s." % cell_name(cursor), "yellow")
                    continue
                self.cursors[seat] = cursor
                return cursor
            elif key in ("Q", "q", "ESC"):
                return None
            elif key == "CTRL_C":
                return None
            elif key and len(key) == 1:
                up = key.upper()
                if up in COLS:
                    cursor = (cursor[0], COLS.index(up))
                elif key in "123456789":
                    cursor = (int(key) - 1, cursor[1])
                elif key == "0" and SIZE >= 10:
                    cursor = (9, cursor[1])

    def run(self):
        for seat in (0, 1):
            self.handoff(seat)
            if not self.setup_board(seat):
                return "abandoned"

        seat = 0
        while True:
            self.handoff(seat)
            foe = self.boards[next_seat(seat)]

            if not supports_cursor_ui():
                print(render_boards(self.boards[seat], foe))

            pos = self.get_shot(seat)
            if pos is None:
                return "abandoned"

            res = self.fire_shells(seat, [pos])
            hit, ship, sunk = res[0]
            burst_shot(pos, hit, ship, sunk, opp=False)
            print("  " + shot_msg_player(pos, *res[0]))

            if foe.all_sunk():
                print()
                for line in big_banner("PLAYER %d WINS" % (seat + 1), "green"):
                    print("  " + line)
                return "p1" if seat == 0 else "p2"

            if supports_cursor_ui():
                print()
                print("  Press Enter to end your turn...")
                try:
                    with KeyReader() as kr:
                        while True:
                            k = kr.get_key()
                            if k == "ENTER":
                                break
                            if k in ("Q", "q", "ESC", "CTRL_C"):
                                return "abandoned"
                except Quit:
                    return "abandoned"
            else:
                ask("Press Enter to end your turn > ")

            seat = next_seat(seat)


# ----------------------------------------------------------------------------
# LAN matchmaking / chat / anti-cheat
# ----------------------------------------------------------------------------

LAN_VERSION = 1
APP_ID = "battleships-lan"
LAN_PORT_RANGE = 20
DEFAULT_LAN_PORT = 48785
HEARTBEAT_INTERVAL = 2.0
HEARTBEAT_TIMEOUT = 10.0
REQUEST_TIMEOUT = 20.0
CHAT_HISTORY_LIMIT = 200
MAX_NET_LINE = 65536

LAN_HELP = """
LAN LOBBY COMMANDS
  list                 show available LAN players
  requests             show incoming match requests
  request <#|IP>       send match request
  accept <#>           accept an incoming request
  reject <#>           reject an incoming request
  cancel               cancel outgoing request / pending match
  pref normal|salvo    set your mode preference
  name <name>          set your display name
  password <secret>    enable password-authenticated lobby
  password             clear lobby password
  anticheat cell       enable per-cell commitment anti-cheat preference
  anticheat off        disable per-cell commitment anti-cheat preference
  status               show LAN security / score status
  say <message>        public LAN chat
  tell <#> <message>   private chat to a listed player
  chat                 show chat history
  start                start pending match, if any
  help                 show this help
  quit                 leave LAN mode

IN-GAME COMMANDS
  B7                   fire at B7 (typed fallback)
  board                redraw boards
  say <message>        chat with opponent
  chat                 show chat history
  surrender            surrender the match
  quit                 quit / surrender

INTERACTIVE IN-GAME CONTROLS
  Arrows / WASD        move cursor
  Enter / Space        fire at cursor
  A–J                  jump to column
  1–9, 0               jump to row
  T                    talk (send a chat message)
  L                    chat history
  Q / Esc              surrender
"""

LAN_PLACE_HELP = ""
LAN_SHOT_HELP = ""


def sign_obj(obj, key=None):
    obj = dict(obj)
    obj.pop("mac", None)
    if key is not None:
        payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        obj["mac"] = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return obj


def send_json_obj(sock, obj, key=None, lock=None):
    obj = sign_obj(obj, key)
    data = json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n"
    raw = data.encode("utf-8")
    if lock is not None:
        with lock:
            sock.sendall(raw)
    else:
        sock.sendall(raw)


def parse_json_line(line, key=None):
    if not line:
        return None
    try:
        obj = json.loads(line.strip())
    except ValueError:
        return None

    if key is not None:
        mac = obj.pop("mac", None)
        payload = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if mac is None or not hmac.compare_digest(expected, str(mac)):
            return None

    return obj


def normalize_board_reveal(reveal):
    if not isinstance(reveal, dict):
        return None

    salt = reveal.get("salt")
    ships = reveal.get("ships")

    if not isinstance(salt, str) or not isinstance(ships, dict):
        return None

    if set(ships.keys()) != set(SHIP_LEN.keys()):
        return None

    norm = {"salt": salt, "ships": {}}

    for name, length in FLEET:
        raw = ships.get(name)
        if not isinstance(raw, list) or len(raw) != length:
            return None

        pts = []
        for p in raw:
            if not isinstance(p, (list, tuple)) or len(p) != 2:
                return None
            try:
                r = int(p[0])
                c = int(p[1])
            except Exception:
                return None
            if not inside((r, c)):
                return None
            pts.append((r, c))

        if len(set(pts)) != length:
            return None

        pts.sort()
        norm["ships"][name] = [[r, c] for r, c in pts]

    return norm


def reveal_payload_json(norm):
    return json.dumps(norm, sort_keys=True, separators=(",", ":"))


def board_commit_hash(norm):
    return hashlib.sha256(reveal_payload_json(norm).encode("utf-8")).hexdigest()


def make_board_reveal(board, salt):
    ships = {}
    for name, cells in board.ship_cells.items():
        ships[name] = sorted([list(p) for p in cells])
    return {"ships": ships, "salt": salt}


def reveal_to_board(reveal):
    norm = normalize_board_reveal(reveal)
    if norm is None:
        return None

    b = Board()
    for name, length in FLEET:
        pts = [tuple(p) for p in norm["ships"][name]]
        rows = sorted(set(r for r, c in pts))
        cols = sorted(set(c for r, c in pts))

        if len(rows) == 1:
            r = rows[0]
            if cols != list(range(cols[0], cols[0] + length)):
                return None
            start = (r, cols[0])
            horizontal = True
        elif len(cols) == 1:
            c = cols[0]
            if rows != list(range(rows[0], rows[0] + length)):
                return None
            start = (rows[0], c)
            horizontal = False
        else:
            return None

        cells, err = b.check_placement(length, start[0], start[1], horizontal)
        if err:
            return None
        if set(cells) != set(pts):
            return None

        b.place(name, cells)

    return b


def verify_shot_log(reveal, log):
    b = reveal_to_board(reveal)
    if b is None:
        return False

    for entry in log:
        if not isinstance(entry, dict):
            return False

        p = entry.get("pos")
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return False

        try:
            pos = (int(p[0]), int(p[1]))
        except Exception:
            return False

        if pos in b.shots:
            return False

        hit, ship, sunk = b.fire(pos)
        if bool(entry.get("hit")) != hit:
            return False

        rep = entry.get("sunk_len", 0)
        if rep is None or rep is False:
            rep = 0
        try:
            rep = int(rep)
        except Exception:
            return False

        if sunk:
            if rep != SHIP_LEN[ship]:
                return False
        else:
            if rep != 0:
                return False

    return True


def derive_match_params(init_id, init_nonce, init_pref,
                        accept_id, accept_nonce, accept_pref,
                        extra_key=None):
    init_pref = "salvo" if init_pref == "salvo" else "single"
    accept_pref = "salvo" if accept_pref == "salvo" else "single"

    items = sorted([(str(init_id), str(init_nonce)),
                    (str(accept_id), str(accept_nonce))])
    material = "|".join([items[0][0], items[0][1], items[1][0], items[1][1]])
    digest = hashlib.sha256(material.encode("utf-8")).digest()

    if init_pref == accept_pref:
        mode = init_pref
    else:
        mode = "salvo" if digest[0] & 1 else "single"

    ids = sorted([str(init_id), str(accept_id)])
    first_id = ids[digest[1] % 2]
    match_id = hashlib.sha256(digest + b"match").hexdigest()[:16]

    key_material = digest + b"key"
    if extra_key:
        key_material += extra_key

    key = hashlib.sha256(key_material).digest()
    return mode, first_id, match_id, key, digest.hex()


def lengths_text(lengths):
    if not lengths:
        return "none"
    return ", ".join(str(x) for x in sorted(lengths, reverse=True))


def lan_shot_msg_player(pos, hit, sunk_len):
    cell = cell_name(pos)
    if sunk_len:
        return paint(">>> SUNK! You destroyed an enemy ship of length %d with %s!" % (sunk_len, cell),
                     "green", "bold")
    if hit:
        return paint(">>> HIT at %s!" % cell, "yellow", "bold")
    return "You fired at %s: miss." % cell


def lan_shot_msg_opp(pos, hit, ship, sunk):
    cell = cell_name(pos)
    if sunk:
        return paint("Opponent fires at %s: SUNK your %s!" % (cell, ship), "red", "bold")
    if hit:
        return paint("Opponent fires at %s: hit on your %s." % (cell, ship), "red")
    return "Opponent fires at %s: miss." % cell


# ----------------------------------------------------------------------------
# Per-cell commitment anti-cheat helpers
# ----------------------------------------------------------------------------

def cell_key(pos):
    return "%d,%d" % pos


def cell_commit_hash(r, c, bit, salt):
    payload = json.dumps([int(r), int(c), int(bit), str(salt)], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_cell_commitments(board):
    salts = {}
    commitments = {}

    for r in range(SIZE):
        for c in range(SIZE):
            bit = 1 if board.cells[r][c] else 0
            salt = secrets.token_hex(8)
            salts[(r, c)] = salt
            commitments[cell_key((r, c))] = cell_commit_hash(r, c, bit, salt)

    root_payload = json.dumps(commitments, sort_keys=True, separators=(",", ":"))
    root = hashlib.sha256(root_payload.encode("utf-8")).hexdigest()
    return salts, commitments, root


def verify_cell_root(commitments, root):
    if not isinstance(commitments, dict) or not isinstance(root, str):
        return False
    payload = json.dumps(commitments, sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected, root)


def verify_cell_reveal(commitments, pos, bit, salt):
    if not isinstance(commitments, dict):
        return False

    try:
        bit = int(bit)
    except Exception:
        return False

    if bit not in (0, 1) or not isinstance(salt, str):
        return False

    key = cell_key(pos)
    expected = commitments.get(key)
    if not isinstance(expected, str):
        return False

    actual = cell_commit_hash(pos[0], pos[1], bit, salt)
    return hmac.compare_digest(expected, actual)


def all_lines_through(pos, length):
    r, c = pos
    lines = []
    for dr, dc in ((0, 1), (1, 0)):
        for k in range(length):
            cells = [(r + dr * (i - k), c + dc * (i - k)) for i in range(length)]
            if all(inside(p) for p in cells):
                lines.append(cells)
    return lines


def cells_form_line(cells):
    cells = list(set(cells))
    if not cells:
        return False

    rows = sorted(set(r for r, c in cells))
    cols = sorted(set(c for r, c in cells))

    if len(rows) == 1:
        cs = sorted(cols)
        return cs == list(range(cs[0], cs[0] + len(cells)))

    if len(cols) == 1:
        rs = sorted(rows)
        return rs == list(range(rs[0], rs[0] + len(cells)))

    return False


def bits_form_fleet(cell_set):
    lengths = list(FLEET_LENGTHS)
    unused = set(cell_set)

    def rec(lengths_left, unused_cells):
        if not lengths_left:
            return not unused_cells
        if not unused_cells:
            return False

        p = min(unused_cells)
        seen_lengths = set()

        for i, length in enumerate(lengths_left):
            if length in seen_lengths:
                continue
            seen_lengths.add(length)

            for line in all_lines_through(p, length):
                line_set = frozenset(line)
                if line_set <= unused_cells:
                    next_lengths = lengths_left[:i] + lengths_left[i + 1:]
                    if rec(next_lengths, unused_cells - line_set):
                        return True

        return False

    return rec(lengths, unused)


def board_from_bits(cell_set):
    b = Board()
    items = list(FLEET)
    unused = set(cell_set)

    def rec(items_left, unused_cells):
        if not items_left:
            return not unused_cells
        if not unused_cells:
            return False

        p = min(unused_cells)
        seen_lengths = set()

        for idx, (name, length) in enumerate(items_left):
            if length in seen_lengths:
                continue
            seen_lengths.add(length)

            for line in all_lines_through(p, length):
                line_set = frozenset(line)
                if line_set <= unused_cells:
                    next_items = items_left[:idx] + items_left[idx + 1:]
                    if rec(next_items, unused_cells - line_set):
                        b.place(name, list(line))
                        return True

        return False

    if rec(items, unused):
        return b
    return None


def verify_cell_final(commitments, reveal):
    if not isinstance(commitments, dict) or not isinstance(reveal, dict):
        return False, "bad reveal structure", set()

    if len(reveal) != SIZE * SIZE:
        return False, "bad reveal size", set()

    ship_cells = set()

    for r in range(SIZE):
        for c in range(SIZE):
            key = cell_key((r, c))
            item = reveal.get(key)

            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return False, "bad reveal entry", set()

            bit, salt = item

            try:
                bit = int(bit)
            except Exception:
                return False, "bad reveal bit", set()

            if bit not in (0, 1) or not isinstance(salt, str):
                return False, "bad reveal value", set()

            if not verify_cell_reveal(commitments, (r, c), bit, salt):
                return False, "commitment mismatch", set()

            if bit:
                ship_cells.add((r, c))

    if not bits_form_fleet(ship_cells):
        return False, "revealed bits do not form a legal fleet", ship_cells

    return True, "ok", ship_cells


# ----------------------------------------------------------------------------
# LAN rendering
# ----------------------------------------------------------------------------

def remote_char(k, r, c, last=None, extra_sunk=None):
    pos = (r, c)

    if extra_sunk is not None and pos in extra_sunk:
        ch = paint("#", "green", "bold")
    elif pos in k.miss:
        ch = paint("o", "white")
    elif pos in k.sunk:
        ch = paint("#", "green", "bold")
    elif pos in k.hit:
        ch = paint("X", "yellow", "bold")
    else:
        ch = paint(".", "grey")

    if last == pos:
        ch = _highlight(ch)

    return ch


def render_lan_boards(player, k, last_player=None, last_opp=None,
                      extra_sunk=None, cursor=None):
    left_rows = [_board_header()] + [
        "%2d  " % (r + 1) + " ".join(own_char(player, r, c, last=last_opp) for c in range(SIZE))
        for r in range(SIZE)
    ]
    right_rows = [_board_header()]
    for r in range(SIZE):
        cells = []
        for c in range(SIZE):
            ch = remote_char(k, r, c, last=last_player, extra_sunk=extra_sunk)
            if cursor == (r, c):
                ch = cursor_reverse(ch)
            cells.append(ch)
        right_rows.append("%2d  " % (r + 1) + " ".join(cells))

    left_box = boxed_panel(paint("YOUR FLEET", "bold"), left_rows)
    right_box = boxed_panel(paint("ENEMY WATERS", "bold"), right_rows)
    return "\n".join("  " + line for line in side_by_side(left_box, right_box, gap="   "))


PROTOCOL_TYPES = {
    "ready",
    "shot",
    "salvo",
    "shot_result",
    "salvo_result",
    "reveal",
    "end",
    "abort",
    "cheat",
}


class MatchConnection:
    # Logical order: __init__ -> public start/send/close -> private _notify/_mark/_read/_heartbeat
    def __init__(self, client, sock, peer_id, peer_name, mode, first_id,
                 my_id, match_id, key, reader=None):
        self.client = client
        self.sock = sock
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.mode = mode
        self.first_id = first_id
        self.my_id = my_id
        self.match_id = match_id
        self.key = key
        self.reader = reader
        self.cell_anticheat = False

        self.send_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.queue = queue.Queue()
        self.closed = False
        self.started = False
        self.last_seen = time.time()
        self.async_handler = None
        self.disconnect_notified = False

        try:
            self.sock.settimeout(None)
        except OSError:
            pass

    def start(self):
        if self.started:
            return
        self.started = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def send(self, obj):
        if self.closed:
            return False
        try:
            send_json_obj(self.sock, obj, key=self.key, lock=self.send_lock)
            return True
        except OSError:
            self._mark_closed_from_network()
            return False

    def close(self, notify=False):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True

        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass

        try:
            self.sock.close()
        except OSError:
            pass

        if notify:
            self._notify_disconnect()

    def _notify_disconnect(self):
        if self.disconnect_notified:
            return
        self.disconnect_notified = True
        evt = {"type": "disconnect"}

        try:
            self.queue.put(evt)
        except Exception:
            pass

        if self.async_handler:
            try:
                self.async_handler(evt)
            except Exception:
                pass
        else:
            try:
                self.client.print_now("LAN connection lost.")
            except Exception:
                pass

    def _mark_closed_from_network(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True

        if self.reader is not None:
            try:
                self.reader.close()
            except Exception:
                pass

        try:
            self.sock.close()
        except OSError:
            pass

        self._notify_disconnect()

    def _read_loop(self):
        f = self.reader
        if f is None:
            try:
                f = self.sock.makefile("r", encoding="utf-8")
            except OSError:
                self._mark_closed_from_network()
                return

        self.reader = f
        bad_lines = 0

        while not self.closed:
            try:
                line = f.readline(MAX_NET_LINE)
            except OSError:
                break

            if not line:
                break

            if not line.endswith("\n"):
                break

            obj = parse_json_line(line, key=self.key)
            if obj is None:
                bad_lines += 1
                if bad_lines > 100:
                    break
                continue

            self.last_seen = time.time()
            t = obj.get("type")

            if t == "ping":
                self.send({"type": "pong"})
                continue
            if t == "pong":
                continue

            if t == "chat":
                try:
                    self.client.handle_match_chat(self.peer_name, str(obj.get("text", "")))
                except Exception:
                    pass
                continue

            if t in ("surrender", "disconnect"):
                if self.async_handler:
                    try:
                        self.async_handler(obj)
                    except Exception:
                        pass
                try:
                    self.queue.put(obj)
                except Exception:
                    pass
                continue

            try:
                self.queue.put(obj)
            except Exception:
                pass

        self._mark_closed_from_network()

    def _heartbeat_loop(self):
        while not self.closed:
            time.sleep(HEARTBEAT_INTERVAL)
            if self.closed:
                break
            if time.time() - self.last_seen > HEARTBEAT_TIMEOUT:
                self._mark_closed_from_network()
                break
            self.send({"type": "ping"})


class LANGame:
    # Logical order: __init__ -> public setup/exchange/show/get_shot/apply/take/handle/play/run -> private _async/_opponent/_send/_wait/_verify/show_finish/_finalize
    def __init__(self, client, conn):
        self.client = client
        self.conn = conn
        self.mode = conn.mode
        self.player = Board()
        self.enemy = Board()
        self.pk = Knowledge()

        self.my_id = conn.my_id
        self.peer_name = conn.peer_name
        self.my_turn = (conn.first_id == conn.my_id)

        self.turn = 1
        self.stats = {
            "shots": 0, "hits": 0, "hints": 0, "opp_shots": 0,
            "opp_hits": 0, "coach_opt": 0, "coach_total": 0,
        }

        self.salt = secrets.token_hex(16)
        self.commit = None
        self.opp_commit = None
        self.opp_reveal = None
        self.my_shot_log = []

        self.last_player = None
        self.last_opp = None
        self.cursor = None

        self.result = None
        self.finished = False
        self.local_surrendered = False
        self.opponent_surrendered = False
        self.opponent_disconnected = False
        self.interrupt_msg = None
        self.anti_cheat_msg = None
        self.finish_notes = []
        self.win_kind = None

        self.cell_salt = None
        self.cell_commitments = None
        self.cell_root = None

        self.opp_cell_commitments = None
        self.opp_cell_root = None
        self.opp_cell_reveal = None

        self.verified_sunk_cells = set()
        self.final_ship_cells = None

        self.conn.async_handler = self._async_event

    def _async_event(self, evt):
        t = evt.get("type")
        if t == "disconnect" and not self.opponent_disconnected:
            self.opponent_disconnected = True
            self.client.print_now(paint("Opponent connection lost. You win.", "green", "bold"))
        elif t == "surrender" and not self.opponent_surrendered:
            self.opponent_surrendered = True
            self.client.print_now(paint("%s surrendered. You win." % self.peer_name, "green", "bold"))

    def check_interrupt(self):
        if self.local_surrendered:
            self.result = "loss"
            return True
        if self.opponent_disconnected:
            self.result = "win"
            self.interrupt_msg = "Opponent disconnected."
            return True
        if self.opponent_surrendered:
            self.result = "win"
            self.interrupt_msg = "Opponent surrendered."
            return True
        if self.conn.closed:
            self.opponent_disconnected = True
            self.result = "win"
            self.interrupt_msg = "Opponent disconnected."
            return True
        return False

    def handle_critical(self, obj):
        t = obj.get("type")
        if t == "disconnect":
            self.opponent_disconnected = True
            self.result = "win"
            self.interrupt_msg = "Opponent disconnected."
        elif t == "surrender":
            self.opponent_surrendered = True
            self.result = "win"
            self.interrupt_msg = "Opponent surrendered."
        else:
            self.result = "win"
            self.interrupt_msg = "Opponent aborted the match."
        return False

    def _opponent_cheat(self, reason):
        self.anti_cheat_msg = "Opponent protocol violation: %s." % reason
        self.result = "win"
        try:
            self.conn.send({"type": "cheat", "reason": reason})
        except Exception:
            pass

    def send_surrender(self):
        if not self.local_surrendered:
            self.local_surrendered = True
            self.conn.send({"type": "surrender"})

    def send_chat(self, text):
        text = text.strip()[:500]
        if not text:
            return
        self.conn.send({"type": "chat", "text": text})
        self.client.add_chat(paint("[You] %s" % text, "cyan"))

    def wait_event(self, accept, timeout=None):
        deadline = time.time() + timeout if timeout is not None else None

        while True:
            if self.conn.closed:
                self.opponent_disconnected = True
                return {"type": "disconnect"}

            try:
                obj = self.conn.queue.get(timeout=0.2)
            except queue.Empty:
                if deadline is not None and time.time() > deadline:
                    return None
                continue

            t = obj.get("type")

            if t == "disconnect":
                self.opponent_disconnected = True
                return obj

            if t == "surrender":
                self.opponent_surrendered = True
                return obj

            if t == "reveal":
                self.opp_reveal = obj.get("board")
                self.opp_cell_reveal = obj.get("cell_reveal")

            if accept(obj):
                return obj

            if t in PROTOCOL_TYPES:
                return obj

    def _sync_pk_sunk(self):
        if self.verified_sunk_cells:
            self.pk.sunk.update(self.verified_sunk_cells)
            self.pk.hit.difference_update(self.verified_sunk_cells)

    def add_cell_proof(self, msg, pos, hit, ship, sunk):
        if self.conn.cell_anticheat and self.cell_salt is not None:
            msg["cell_bit"] = 1 if hit else 0
            msg["cell_salt"] = self.cell_salt[pos]

            if sunk and ship is not None:
                msg["sunk_ship"] = [
                    {"pos": list(p), "salt": self.cell_salt[p]}
                    for p in self.player.ship_cells[ship]
                ]

    def verify_remote_cell_result(self, obj, pos):
        if not self.conn.cell_anticheat:
            try:
                sunk_len = int(obj.get("sunk_len") or 0)
            except Exception:
                sunk_len = 0
            return bool(obj.get("hit")), sunk_len, None, None

        bit = obj.get("cell_bit")
        salt = obj.get("cell_salt")

        try:
            bit = int(bit)
        except Exception:
            bit = None

        if bit not in (0, 1) or not isinstance(salt, str):
            return None, None, None, "missing cell reveal"

        if bool(obj.get("hit")) != bool(bit):
            return None, None, None, "cell reveal disagrees with reported hit"

        if not verify_cell_reveal(self.opp_cell_commitments, pos, bit, salt):
            return None, None, None, "bad cell commitment"

        hit = bool(bit)

        try:
            sunk_len = int(obj.get("sunk_len") or 0)
        except Exception:
            sunk_len = 0

        if sunk_len:
            sunk_ship = obj.get("sunk_ship")
            if not isinstance(sunk_ship, list) or len(sunk_ship) != sunk_len:
                return None, None, None, "bad sunk ship reveal"

            cells = []
            for item in sunk_ship:
                try:
                    p = (int(item["pos"][0]), int(item["pos"][1]))
                    s = str(item["salt"])
                except Exception:
                    return None, None, None, "bad sunk ship entry"

                if not inside(p):
                    return None, None, None, "sunk ship cell outside board"

                if not verify_cell_reveal(self.opp_cell_commitments, p, 1, s):
                    return None, None, None, "bad sunk ship commitment"

                cells.append(p)

            if not cells_form_line(cells):
                return None, None, None, "sunk ship cells do not form a line"

            if sunk_len not in self.pk.remaining:
                return None, None, None, "impossible sunk length"

            for p in cells:
                if p == pos:
                    continue
                if self.enemy.shots.get(p) is not True:
                    return None, None, None, "sunk ship contains unhit cell"

            if any(p in self.verified_sunk_cells for p in cells):
                return None, None, None, "sunk ship overlaps previous sunk ship"

            return hit, sunk_len, cells, None

        else:
            if obj.get("sunk_ship"):
                return None, None, None, "unexpected sunk ship reveal"
            return hit, 0, None, None

    def setup_local_fleet(self):
        if not supports_cursor_ui():
            return self._setup_typed()

        print()
        for line in big_banner("FLEET SETUP (LAN)", "bold"):
            print("  " + line)
        print()

        choice = select_menu(
            paint("FLEET SETUP (LAN)", "bold"),
            ["Place my ships by hand", "Random layout"],
            start_idx=_MENU_STATE.last_setup_choice,
        )
        _MENU_STATE.last_setup_choice = choice

        if choice == 1:
            self.player.place_randomly(FLEET)
            clear()
            print(render_own(self.player))
            print()
            print("Random layout shown above.")
            with KeyReader() as kr:
                while True:
                    print("  Enter = start · R = reroll · Q = surrender")
                    key = kr.get_key()
                    if key == "ENTER":
                        return True
                    if key in ("R", "r"):
                        while self.player.undo():
                            pass
                        self.player.place_randomly(FLEET)
                        clear()
                        print(render_own(self.player))
                        print()
                        continue
                    if key in ("Q", "q", "ESC", "CTRL_C"):
                        if confirm("Surrender this match?"):
                            self.send_surrender()
                            self.result = "loss"
                            return False

        if interactive_place_fleet(self.player) is None:
            return "QUIT"
        return True

    def _setup_typed(self):
        print("\nFLEET SETUP (LAN)")
        choice = None

        while True:
            if self.check_interrupt():
                return False

            raw0 = ask("  1) Place my ships by hand  2) Random layout > ")
            raw = raw0.strip().lower()

            if self.check_interrupt():
                return False

            if raw in ("1", "2"):
                choice = raw
                break

            if raw.startswith("say "):
                self.send_chat(raw0.strip()[4:])
            elif raw == "chat":
                self.client.print_chat_history()
            elif raw in ("quit", "q", "exit"):
                if confirm("Surrender this match?"):
                    self.send_surrender()
                    self.result = "loss"
                    return False
            else:
                print("Enter 1 or 2.")

        if choice == "2":
            self.player.place_randomly(FLEET)
            return True

        board = self.player
        while True:
            if self.check_interrupt():
                return False

            print("\n" + render_own(board))
            n = len(board.order)

            if n == len(FLEET):
                raw0 = ask("Fleet ready. Enter = start | undo | random | say | chat | quit > ")
            else:
                name, length = FLEET[n]
                raw0 = ask("[%d/%d] Place %s (%d). Example: A1 H | random | undo | say | chat | quit > "
                           % (n + 1, len(FLEET), name, length))

            raw = raw0.strip().lower()
            if self.check_interrupt():
                return False

            if raw in ("quit", "q", "exit"):
                if confirm("Surrender this match?"):
                    self.send_surrender()
                    self.result = "loss"
                    return False
                continue

            if raw.startswith("say "):
                self.send_chat(raw0.strip()[4:])
                continue

            if raw == "chat":
                self.client.print_chat_history()
                continue

            if raw == "undo":
                undone = board.undo()
                print("  Removed %s." % undone if undone else "  Nothing to undo.")
                continue

            if raw in ("random", "auto", "r"):
                while board.undo():
                    pass
                board.place_randomly(FLEET)
                continue

            if raw == "" and n == len(FLEET):
                return True

            if raw == "":
                continue

            if n < len(FLEET):
                name, length = FLEET[n]
                parsed = parse_placement(raw)
                if not parsed:
                    print("  Can't read that. Use START DIRECTION, e.g. A1 H or A1 V.")
                    continue
                (r, c), horizontal = parsed
                cells, err = board.check_placement(length, r, c, horizontal)
                if err:
                    print("  Can't place %s at %s %s: %s." %
                          (name, cell_name((r, c)), "H" if horizontal else "V", err))
                else:
                    board.place(name, cells)
            else:
                print("  Press Enter to start, or type undo / random.")

    def exchange_ready(self):
        if self.conn.cell_anticheat:
            self.cell_salt, self.cell_commitments, self.cell_root = make_cell_commitments(self.player)
            self.conn.send({
                "type": "ready",
                "cell_anticheat": True,
                "cell_root": self.cell_root,
                "cell_commitments": self.cell_commitments,
            })
        else:
            norm = normalize_board_reveal(make_board_reveal(self.player, self.salt))
            if norm is None:
                self.result = "abandoned"
                return False
            self.commit = board_commit_hash(norm)
            self.conn.send({"type": "ready", "commit": self.commit})

        while True:
            if self.check_interrupt():
                return False

            obj = self.wait_event(lambda o: o.get("type") == "ready")
            if obj is None:
                continue

            t = obj.get("type")
            if t in ("disconnect", "surrender", "abort", "cheat"):
                return self.handle_critical(obj)

            if t == "ready":
                if self.conn.cell_anticheat:
                    if not obj.get("cell_anticheat"):
                        self._opponent_cheat("expected per-cell ready")
                        return False

                    commitments = obj.get("cell_commitments")
                    root = obj.get("cell_root")

                    if not isinstance(commitments, dict) or len(commitments) != SIZE * SIZE:
                        self._opponent_cheat("bad cell commitments")
                        return False

                    if not isinstance(root, str) or not verify_cell_root(commitments, root):
                        self._opponent_cheat("bad cell root")
                        return False

                    self.opp_cell_commitments = commitments
                    self.opp_cell_root = root
                else:
                    self.opp_commit = obj.get("commit")
                    if not isinstance(self.opp_commit, str) or len(self.opp_commit) != 64:
                        self._opponent_cheat("bad ready commit")
                        return False

                return True

            self._opponent_cheat("unexpected message before ready")
            return False

    def status_lines(self):
        s = self.stats
        acc = "%d%%" % (100 * s["hits"] // s["shots"]) if s["shots"] else "-"
        mode_name = "Salvo" if self.mode == "salvo" else "Normal"
        return [
            "%s %d  ·  %s  ·  %s  ·  You: %d shots, %d hits (%s)"
            % (paint("Turn", "bold"), self.turn, self.peer_name, mode_name, s["shots"], s["hits"], acc),
            "%s  %s" % (paint("Your fleet:  ", "bold"), fleet_damage_text(self.player)),
            "%s %s" % (paint("Enemy lengths:", "bold"), lengths_text(self.pk.remaining)),
        ]

    def show(self, notes, cursor=None, extras=None):
        clear()
        extra_sunk = self.verified_sunk_cells if self.conn.cell_anticheat else None

        print(render_lan_boards(
            self.player, self.pk,
            last_player=self.last_player,
            last_opp=self.last_opp,
            extra_sunk=extra_sunk,
            cursor=cursor,
        ))

        print()
        for line in boxed_panel(paint("STATUS", "bold"), self.status_lines()):
            print("  " + line)
        if extras:
            print()
            for line in extras:
                print("  " + line)
        print()
        print_log(notes)

    def _first_untried(self):
        for r in range(SIZE):
            for c in range(SIZE):
                if (r, c) not in self.enemy.shots:
                    return (r, c)
        return (0, 0)

    def _render_shot_cursor(self, cursor, notes, extra="", salvo=False, remaining=1, current=None):
        extras = []
        if salvo:
            extras.append(paint("Salvo shell %d of %d" % (len(current or []) + 1, remaining), "cyan"))
        if extra:
            extras.append(extra)
        self.show(notes, cursor=cursor, extras=extras)
        print()
        print("  " + paint("─" * 60, "grey"))
        print("  Arrows move · Enter fire · A–J col · 1–0 row · T talk · L chat · Q surrender")

    def _get_shot_typed(self, notes, salvo=False, remaining=1, current=None):
        current = current or []
        while True:
            if self.check_interrupt():
                return None

            if salvo:
                prompt = "Salvo shell %d of %d (e.g. B7 | say | surrender) > " % (len(current) + 1, remaining)
            else:
                prompt = "Your shot (e.g. B7 | board | say | surrender) > "

            raw0 = ask(prompt)
            raw = raw0.strip().lower()

            if self.check_interrupt():
                return None

            if raw == "board":
                self.show(notes)
            elif raw.startswith("say "):
                self.send_chat(raw0.strip()[4:])
            elif raw == "chat":
                self.client.print_chat_history()
            elif raw == "surrender":
                if confirm("Surrender this match?"):
                    self.send_surrender()
                    self.result = "loss"
                    return None
            elif raw in ("quit", "q", "exit"):
                if confirm("Surrender this match?"):
                    self.send_surrender()
                    self.result = "loss"
                    return None
            else:
                pos = parse_cell(raw)
                if pos is None:
                    print("  %s." % shot_error(raw))
                elif pos in self.enemy.shots:
                    print("  You already fired at %s. Pick another square." % cell_name(pos))
                else:
                    return pos

    def _get_shot_cursor(self, notes, salvo=False, remaining=1, current=None):
        current = current or []
        cursor = self.cursor if self.cursor is not None else self._first_untried()
        extra = ""

        while True:
            if self.check_interrupt():
                return None

            self._render_shot_cursor(cursor, notes, extra, salvo, remaining, current)
            extra = ""

            try:
                with KeyReader() as kr:
                    key = kr.get_key()
            except Quit:
                return None

            if self.check_interrupt():
                return None

            if key == "UP":
                cursor = (max(0, cursor[0] - 1), cursor[1])
            elif key == "DOWN":
                cursor = (min(SIZE - 1, cursor[0] + 1), cursor[1])
            elif key == "LEFT":
                cursor = (cursor[0], max(0, cursor[1] - 1))
            elif key == "RIGHT":
                cursor = (cursor[0], min(SIZE - 1, cursor[1] + 1))
            elif key in ("ENTER", " "):
                if cursor in self.enemy.shots:
                    extra = paint("Already fired at %s." % cell_name(cursor), "yellow")
                    continue
                if cursor in current:
                    extra = paint("Already targeted that square this salvo.", "yellow")
                    continue
                self.cursor = cursor
                return cursor
            elif key in ("Q", "q", "ESC"):
                if confirm("Surrender this match?"):
                    self.send_surrender()
                    self.result = "loss"
                    return None
            elif key == "CTRL_C":
                return None
            elif key in ("T", "t"):
                text = ask("  Say to opponent > ").strip()
                if text:
                    self.send_chat(text)
            elif key in ("L", "l"):
                self.client.print_chat_history()
                with KeyReader() as kr:
                    print("  Press any key to continue...")
                    kr.get_key()
            elif key and len(key) == 1:
                up = key.upper()
                if up in COLS:
                    cursor = (cursor[0], COLS.index(up))
                elif key in "123456789":
                    cursor = (int(key) - 1, cursor[1])
                elif key == "0" and SIZE >= 10:
                    cursor = (9, cursor[1])

    def get_shot(self, notes, salvo=False, remaining=1, current=None):
        if not supports_cursor_ui():
            return self._get_shot_typed(notes, salvo=salvo, remaining=remaining, current=current)
        return self._get_shot_cursor(notes, salvo=salvo, remaining=remaining, current=current)

    def apply_remote_result(self, pos, hit, sunk_len, sunk_cells=None):
        if not inside(pos):
            self._opponent_cheat("result outside board")
            return False

        if pos in self.enemy.shots:
            self._opponent_cheat("duplicate shot result")
            return False

        if not hit and sunk_len:
            self._opponent_cheat("miss cannot sink a ship")
            return False

        if sunk_len and sunk_len not in self.pk.remaining:
            self._opponent_cheat("impossible sunk length")
            return False

        self.enemy.shots[pos] = bool(hit)
        self.pk.record(pos, bool(hit), sunk_len if sunk_len else None)

        if sunk_cells:
            self.verified_sunk_cells.update(sunk_cells)
            self._sync_pk_sunk()

        self.my_shot_log.append({
            "pos": list(pos),
            "hit": bool(hit),
            "sunk_len": int(sunk_len or 0),
        })

        self.stats["shots"] += 1
        if hit:
            self.stats["hits"] += 1

        return True

    def take_shot_turn(self, notes):
        pos = self.get_shot(notes)
        if pos is None:
            return False

        opt = is_coach_opt(self.pk, pos)
        self.stats["coach_total"] += 1
        if opt:
            self.stats["coach_opt"] += 1

        self.conn.send({"type": "shot", "pos": list(pos)})

        if supports_cursor_ui():
            with Spinner("Waiting for %s" % self.peer_name):
                obj = self.wait_event(lambda o: o.get("type") == "shot_result")
        else:
            obj = self.wait_event(lambda o: o.get("type") == "shot_result")

        if obj is None:
            return True

        t = obj.get("type")
        if t in ("disconnect", "surrender", "abort", "cheat"):
            return self.handle_critical(obj)
        if t != "shot_result":
            self._opponent_cheat("expected shot_result")
            return False

        try:
            rp = obj.get("pos")
            res_pos = (int(rp[0]), int(rp[1]))
        except Exception:
            self._opponent_cheat("bad shot_result position")
            return False

        if res_pos != pos:
            self._opponent_cheat("shot_result position mismatch")
            return False

        hit, sunk_len, sunk_cells, err = self.verify_remote_cell_result(obj, pos)
        if err:
            self._opponent_cheat(err)
            return False

        if not self.apply_remote_result(pos, hit, sunk_len, sunk_cells):
            return False

        burst_shot_lan(pos, hit, sunk_len, opp=False)
        self.last_player = pos
        notes[:] = [lan_shot_msg_player(pos, hit, sunk_len)]
        # Show the player's mark before waiting on the peer's move.
        self.show(notes, cursor=self.cursor)
        return True

    def take_salvo_turn(self, notes):
        n = salvo_size(self.player)
        shots = []

        while len(shots) < n:
            pos = self.get_shot(notes, salvo=True, remaining=n, current=shots)
            if pos is None:
                return False
            if pos in shots:
                continue
            shots.append(pos)

        opts = [is_coach_opt(self.pk, p) for p in shots]
        self.stats["coach_total"] += n
        self.stats["coach_opt"] += sum(1 for x in opts if x)

        self.conn.send({"type": "salvo", "positions": [list(p) for p in shots]})

        if supports_cursor_ui():
            with Spinner("Waiting for %s" % self.peer_name):
                obj = self.wait_event(lambda o: o.get("type") == "salvo_result")
        else:
            obj = self.wait_event(lambda o: o.get("type") == "salvo_result")

        if obj is None:
            return True

        t = obj.get("type")
        if t in ("disconnect", "surrender", "abort", "cheat"):
            return self.handle_critical(obj)
        if t != "salvo_result":
            self._opponent_cheat("expected salvo_result")
            return False

        results = obj.get("results")
        if not isinstance(results, list) or len(results) != n:
            self._opponent_cheat("bad salvo_result length")
            return False

        notes[:] = []
        for i, pos in enumerate(shots):
            try:
                entry = results[i]
                rp = entry.get("pos")
                res_pos = (int(rp[0]), int(rp[1]))
            except Exception:
                self._opponent_cheat("bad salvo_result entry")
                return False

            if res_pos != pos:
                self._opponent_cheat("salvo_result position mismatch")
                return False

            hit, sunk_len, sunk_cells, err = self.verify_remote_cell_result(entry, pos)
            if err:
                self._opponent_cheat(err)
                return False

            if not self.apply_remote_result(pos, hit, sunk_len, sunk_cells):
                return False

            burst_shot_lan(pos, hit, sunk_len, opp=False)
            notes.append(lan_shot_msg_player(pos, hit, sunk_len))

        self.last_player = shots[-1]
        self.show(notes, cursor=self.cursor)
        return True

    def handle_opponent_shot(self, obj, notes):
        try:
            p = obj.get("pos")
            pos = (int(p[0]), int(p[1]))
        except Exception:
            self._opponent_cheat("bad shot position")
            return False

        if not inside(pos) or pos in self.player.shots:
            self._opponent_cheat("invalid shot")
            return False

        hit, ship, sunk = self.player.fire(pos)
        self.stats["opp_shots"] += 1
        if hit:
            self.stats["opp_hits"] += 1
        self.last_opp = pos

        sunk_len = SHIP_LEN[ship] if sunk else 0

        msg = {
            "type": "shot_result",
            "pos": list(pos),
            "hit": hit,
            "sunk_len": sunk_len,
            "game_over": self.player.all_sunk(),
        }
        self.add_cell_proof(msg, pos, hit, ship, sunk)
        self.conn.send(msg)

        burst_shot_lan(pos, hit, sunk_len, opp=True)
        notes[:] = [lan_shot_msg_opp(pos, hit, ship, sunk)]
        return True

    def handle_opponent_salvo(self, obj, notes):
        raw_positions = obj.get("positions")
        if not isinstance(raw_positions, list):
            self._opponent_cheat("bad salvo positions")
            return False

        expected = len(self.pk.remaining)
        if expected <= 0 or len(raw_positions) != expected:
            self._opponent_cheat("bad salvo size")
            return False

        positions = []
        seen = set()

        for p in raw_positions:
            try:
                pos = (int(p[0]), int(p[1]))
            except Exception:
                self._opponent_cheat("bad salvo position")
                return False

            if not inside(pos) or pos in seen or pos in self.player.shots:
                self._opponent_cheat("invalid salvo position")
                return False

            seen.add(pos)
            positions.append(pos)

        results = []
        notes[:] = []

        for pos in positions:
            hit, ship, sunk = self.player.fire(pos)
            self.stats["opp_shots"] += 1
            if hit:
                self.stats["opp_hits"] += 1

            sunk_len = SHIP_LEN[ship] if sunk else 0
            entry = {
                "pos": list(pos),
                "hit": hit,
                "sunk_len": sunk_len,
            }
            self.add_cell_proof(entry, pos, hit, ship, sunk)
            results.append(entry)

            burst_shot_lan(pos, hit, sunk_len, opp=True)
            notes.append(lan_shot_msg_opp(pos, hit, ship, sunk))

        self.last_opp = positions[-1]
        self.conn.send({
            "type": "salvo_result",
            "results": results,
            "game_over": self.player.all_sunk(),
        })
        return True

    def wait_opponent_move(self, notes):
        expected = "salvo" if self.mode == "salvo" else "shot"

        if supports_cursor_ui():
            with Spinner("%s is aiming" % self.peer_name):
                obj = self.wait_event(lambda o: o.get("type") == expected)
        else:
            obj = self.wait_event(lambda o: o.get("type") == expected)

        if obj is None:
            return True

        t = obj.get("type")
        if t in ("disconnect", "surrender", "abort", "cheat"):
            return self.handle_critical(obj)
        if t != expected:
            self._opponent_cheat("unexpected move type")
            return False

        if self.mode == "salvo":
            return self.handle_opponent_salvo(obj, notes)
        return self.handle_opponent_shot(obj, notes)

    def play_loop(self):
        mode_name = "Salvo" if self.mode == "salvo" else "Normal"
        first = "You fire first." if self.my_turn else "%s fires first." % self.peer_name
        notes = [paint("LAN match started. Mode: %s. %s" % (mode_name, first), "bold")]

        while True:
            if self.check_interrupt():
                self.finish_notes = notes
                break

            if not supports_cursor_ui():
                self.show(notes)

            if self.my_turn:
                if self.mode == "salvo":
                    ok = self.take_salvo_turn(notes)
                else:
                    ok = self.take_shot_turn(notes)

                if not ok:
                    self.finish_notes = notes
                    break

                if not self.pk.remaining:
                    self.result = "win"
                    self.finish_notes = notes
                    break

                self.my_turn = False
            else:
                ok = self.wait_opponent_move(notes)
                if not ok:
                    self.finish_notes = notes
                    break

                if self.player.all_sunk():
                    self.result = "loss"
                    self.finish_notes = notes
                    break

                self.my_turn = True
                self.turn += 1

    def run(self):
        try:
            if not self.setup_local_fleet():
                return self._finalize()
            if not self.exchange_ready():
                return self._finalize()
            self.play_loop()
            return self._finalize()
        except Quit:
            if not self.local_surrendered:
                self.send_surrender()
                self.result = "loss"
            return self._finalize()

    def _send_reveal(self):
        if self.conn.cell_anticheat and self.cell_salt is not None:
            cell_reveal = {}
            for r in range(SIZE):
                for c in range(SIZE):
                    bit = 1 if self.player.cells[r][c] else 0
                    cell_reveal[cell_key((r, c))] = [bit, self.cell_salt[(r, c)]]
            self.conn.send({"type": "reveal", "cell_reveal": cell_reveal})
        elif len(self.player.order) == len(FLEET):
            self.conn.send({
                "type": "reveal",
                "board": make_board_reveal(self.player, self.salt)
            })

    def _wait_for_reveal_if_needed(self):
        if self.local_surrendered or self.opponent_disconnected or self.opponent_surrendered:
            return

        has_commit = bool(self.opp_commit) or bool(self.opp_cell_commitments)
        has_reveal = (self.opp_reveal is not None) or (self.opp_cell_reveal is not None)

        if has_commit and not has_reveal:
            self.wait_event(lambda o: o.get("type") == "reveal", timeout=5.0)

    def _verify_anti_cheat(self):
        self.win_kind = None

        if self.local_surrendered:
            self.result = "loss"
            return

        if self.opponent_disconnected:
            self.result = "win"
            self.win_kind = "forfeit"
            self.interrupt_msg = "Opponent disconnected."
            return

        if self.opponent_surrendered:
            self.result = "win"
            self.win_kind = "forfeit"
            self.interrupt_msg = "Opponent surrendered."
            return

        if self.conn.cell_anticheat:
            if self.opp_cell_commitments is not None and self.opp_cell_reveal is not None:
                ok, msg, ship_cells = verify_cell_final(self.opp_cell_commitments,
                                                        self.opp_cell_reveal)
                if ok:
                    self.final_ship_cells = ship_cells
                    self.anti_cheat_msg = "Per-cell anti-cheat: opponent board verified."
                    if self.result == "win":
                        self.win_kind = "verified"
                else:
                    self.result = "win"
                    self.win_kind = "forfeit"
                    self.anti_cheat_msg = "Per-cell anti-cheat failed: %s." % msg

            elif self.opp_cell_commitments is not None and self.opp_cell_reveal is None:
                if self.result == "loss":
                    self.result = "win"
                    self.win_kind = "forfeit"
                    self.anti_cheat_msg = "Opponent failed to reveal board."
                elif self.result == "win":
                    self.win_kind = "forfeit"
                    self.anti_cheat_msg = "Opponent did not reveal board."

            else:
                if self.result == "win":
                    self.win_kind = "forfeit"

            return

        if self.opp_commit and self.opp_reveal is not None:
            norm = normalize_board_reveal(self.opp_reveal)
            if norm is None:
                self.result = "win"
                self.win_kind = "forfeit"
                self.anti_cheat_msg = "Anti-cheat: opponent reveal malformed."
            elif board_commit_hash(norm) != self.opp_commit:
                self.result = "win"
                self.win_kind = "forfeit"
                self.anti_cheat_msg = "Anti-cheat: opponent board does not match commitment."
            elif not verify_shot_log(self.opp_reveal, self.my_shot_log):
                self.result = "win"
                self.win_kind = "forfeit"
                self.anti_cheat_msg = "Anti-cheat: opponent shot responses inconsistent."
            else:
                self.anti_cheat_msg = "Anti-cheat: opponent board verified."
                if self.result == "win":
                    self.win_kind = "verified"

        elif self.opp_commit and self.opp_reveal is None:
            if self.result == "loss":
                self.result = "win"
                self.win_kind = "forfeit"
                self.anti_cheat_msg = "Anti-cheat: opponent failed to reveal board."
            elif self.result == "win":
                self.win_kind = "forfeit"
                self.anti_cheat_msg = "Opponent did not reveal board."

        else:
            if self.result == "win":
                self.win_kind = "forfeit"

    def show_finish(self):
        clear()

        reveal_board = None

        if self.conn.cell_anticheat and self.final_ship_cells:
            reveal_board = board_from_bits(self.final_ship_cells)
            if reveal_board is not None:
                for pos, hit in self.enemy.shots.items():
                    reveal_board.shots[pos] = hit

        elif self.opp_reveal:
            norm = normalize_board_reveal(self.opp_reveal)
            if norm is not None and self.opp_commit and board_commit_hash(norm) == self.opp_commit:
                reveal_board = reveal_to_board(self.opp_reveal)
                if reveal_board is not None:
                    for pos, hit in self.enemy.shots.items():
                        reveal_board.shots[pos] = hit

        if reveal_board is not None:
            print(render_boards(self.player, reveal_board, reveal=True,
                                last_player=self.last_player, last_ai=self.last_opp))
        else:
            extra_sunk = self.verified_sunk_cells if self.conn.cell_anticheat else None
            print(render_lan_boards(self.player, self.pk,
                                    last_player=self.last_player,
                                    last_opp=self.last_opp,
                                    extra_sunk=extra_sunk))

        print()

        if self.result == "win":
            title, color = "V I C T O R Y", "green"
        elif self.result == "loss":
            title, color = "D E F E A T", "red"
        else:
            title, color = "A B A N D O N E D", "yellow"

        for line in big_banner(title, color):
            print("  " + line)
        print()

        if self.interrupt_msg:
            print("  " + self.interrupt_msg)

        if self.result == "win" and self.win_kind == "verified":
            print("  " + paint("(verified win)", "cyan"))
        elif self.result == "win":
            print("  " + paint("(forfeit win)", "cyan"))

        if self.anti_cheat_msg:
            print("  " + self.anti_cheat_msg)
        print()

        for note in self.finish_notes:
            print("    " + note)
        print()

        s = self.stats
        acc = "%d%%" % (100 * s["hits"] // s["shots"]) if s["shots"] else "-"
        summary = [
            "Turns: %d" % self.turn,
            "Your shots:      %d  (%d hits, %s)" % (s["shots"], s["hits"], acc),
            "Opponent shots:  %d  (%d hits)" % (s["opp_shots"], s["opp_hits"]),
            coach_line(s["coach_opt"], s["coach_total"]),
        ]
        for line in boxed_panel(paint("SUMMARY", "bold"), summary):
            print("  " + line)

    def _finalize(self):
        if self.finished:
            return self.result or "abandoned"

        self.finished = True

        if self.result is None:
            self.result = "abandoned"

        self._send_reveal()
        self._wait_for_reveal_if_needed()
        self._verify_anti_cheat()
        self.show_finish()

        try:
            self.conn.send({"type": "end", "reason": self.result})
        except Exception:
            pass

        return self.result


@dataclass
class PeerInfo:
    """Typed peer record (dataclass replaces hand-rolled __init__)."""
    id: str
    name: str
    pref: str
    addr: Any
    tcp_port: int
    state: str = "available"
    cell_anticheat: bool = False
    last_seen: float = field(default_factory=time.time)

    # Backward-compat: allow positional pid alias used by older call sites.
    @classmethod
    def from_parts(cls, pid: str, name: str, pref: str, addr: Any,
                   tcp_port: int, state: str = "available",
                   cell_anticheat: bool = False) -> "PeerInfo":
        return cls(pid, name, pref, addr, tcp_port, state, cell_anticheat)


class LANClient:
    # LANClient is now a thin facade: peer/request/chat/rate-limit state lives
    # in PeerRegistry / ChatLog / TcpRateLimiter (see top of file).
    def __init__(self, port: int = DEFAULT_LAN_PORT) -> None:
        self.id = secrets.token_hex(8)
        self.name = "Player-" + self.id[:4]
        self.pref = "single"

        self.base_port = port
        self.port = port
        self.tcp_port = port

        self.running = False
        self.in_match = False

        self.peers = {}
        self.peer_lock = threading.Lock()

        self.incoming_requests = {}
        self.request_lock = threading.Lock()

        self.outgoing_request = None
        self.out_lock = threading.Lock()

        self.pending_match = None
        self.match = None

        self.chat_history = collections.deque(maxlen=CHAT_HISTORY_LIMIT)
        self.print_lock = threading.Lock()

        self.last_listing = {}
        self.last_req_listing = {}

        self.lobby_key = None
        self.cell_anticheat = False

        self.handshake_threads = 0
        self.handshake_lock = threading.Lock()
        self.max_handshake_threads = 16

        self.tcp_rate = {}
        self.rate_lock = threading.Lock()

        self.max_incoming_requests = 8

        self.lan_score = {
            "win": 0, "loss": 0,
            "verified_win": 0, "forfeit_win": 0,
        }
        # Cohesive collaborators (God-class split):
        self._registry = PeerRegistry()
        self._chat = ChatLog(limit=CHAT_HISTORY_LIMIT)
        self._limiter = TcpRateLimiter()
        self.interactive_lobby = False
        self.notifications = collections.deque(maxlen=20)

        self.udp_sock = None
        self.tcp_sock = None

    @property
    def peer_count(self) -> int:
        """Encapsulated peer count (prefers registry when populated)."""
        try:
            n = self._registry.count
            if n:
                return n
        except Exception:
            pass
        with self.peer_lock:
            return len(self.peers)

    def print_now(self, line):  # type: ignore[no-untyped-def]
        if getattr(self, "interactive_lobby", False):
            if hasattr(self, "notifications"):
                self.notifications.append(line)
            return

        with self.print_lock:
            print(line)

    def add_chat(self, line):  # type: ignore[no-untyped-def]
        full = self._chat.add(line)  # cohesive log owns formatting/bounds
        self.chat_history.append(full)
        self.print_now(full)

    def print_chat_history(self, n=50):
        items = list(self.chat_history)[-n:]
        if not items:
            self.print_now("  No chat messages.")
            return
        for line in items:
            self.print_now("  " + line)

    def handle_match_chat(self, peer_name, text):
        self.add_chat(paint("[%s] %s" % (peer_name, text), "cyan"))

    def start(self):
        self._bind_sockets()
        self.running = True
        threading.Thread(target=self._udp_listen_loop, daemon=True).start()
        threading.Thread(target=self._tcp_accept_loop, daemon=True).start()
        threading.Thread(target=self._beacon_loop, daemon=True).start()

    def stop(self):
        self.running = False

        self.cancel_outgoing()

        with self.request_lock:
            reqs = list(self.incoming_requests.values())
            self.incoming_requests.clear()

        for req in reqs:
            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass
            self._force_close(req.get("sock"))

        if self.pending_match:
            try:
                self.pending_match.send({"type": "abort", "reason": "lobby closed"})
            except Exception:
                pass
            self.pending_match.close()
            self.pending_match = None

        if self.match:
            self.match.close()
            self.match = None

        self._force_close(self.udp_sock)
        self._force_close(self.tcp_sock)

        self.udp_sock = None
        self.tcp_sock = None

    def _bind_sockets(self):
        last_err = None

        for port in range(self.base_port, self.base_port + LAN_PORT_RANGE):
            udp = None
            tcp = None

            try:
                udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

                try:
                    udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                except OSError:
                    pass

                tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

                udp.bind(("", port))
                tcp.bind(("", port))
                tcp.listen(16)

                self.port = port
                self.tcp_port = port
                self.udp_sock = udp
                self.tcp_sock = tcp
                return

            except OSError as e:
                last_err = e

                for s in (udp, tcp):
                    if s is not None:
                        try:
                            s.close()
                        except OSError:
                            pass

        raise last_err

    def _force_close(self, sock):
        if sock is None:
            return

        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

        try:
            sock.close()
        except OSError:
            pass

    def set_password(self, secret):
        secret = secret.strip()

        if not secret:
            self.lobby_key = None
            self.print_now("Lobby password cleared. Lobby is unauthenticated.")
        else:
            self.lobby_key = hashlib.pbkdf2_hmac(
                "sha256",
                secret.encode("utf-8"),
                b"battleships-lan-v1",
                200000
            )
            self.print_now("Lobby password enabled. Peers must use the same password.")

        with self.peer_lock:
            self.peers.clear()

    def _allow_tcp(self, addr) -> bool:  # type: ignore[no-untyped-def]
        # Delegate to cohesive rate limiter; keep legacy dict in sync for compat.
        allowed = self._limiter.allow(addr[0])
        now = time.time()
        with self.rate_lock:
            recent = [t for t in self.tcp_rate.get(addr[0], []) if now - t < 5.0]
            if allowed:
                recent.append(now)
            self.tcp_rate[addr[0]] = recent
        return allowed

    def _tcp_conn_wrapper(self, conn, addr):
        try:
            self._handle_tcp_conn(conn, addr)
        finally:
            with self.handshake_lock:
                self.handshake_threads -= 1

    def _clean_peers(self):
        now = time.time()
        with self.peer_lock:
            for pid in list(self.peers.keys()):
                if now - self.peers[pid].last_seen > 6.0:
                    del self.peers[pid]

    def _clean_requests(self):
        now = time.time()
        expired = []

        with self.request_lock:
            for rid, req in list(self.incoming_requests.items()):
                if now - req["time"] > REQUEST_TIMEOUT:
                    expired.append((rid, req))
                    del self.incoming_requests[rid]

        for rid, req in expired:
            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass
            try:
                req["sock"].close()
            except OSError:
                pass
            self.print_now("Request from %s expired." % req["from_name"])

    def _handle_beacon(self, obj, addr):
        pid = str(obj.get("id", ""))
        if not pid or pid == self.id:
            return

        try:
            tcp_port = int(obj.get("tcp_port", self.port))
        except Exception:
            tcp_port = self.port

        paddr = (addr[0], tcp_port)

        with self.peer_lock:
            p = self.peers.get(pid)
            if p is None:
                p = PeerInfo(pid,
                             str(obj.get("name", "Player")),
                             obj.get("pref", "single"),
                             paddr,
                             tcp_port,
                             obj.get("state", "available"),
                             bool(obj.get("cell_anticheat")))
                self.peers[pid] = p
            else:
                p.name = str(obj.get("name", p.name))
                p.pref = obj.get("pref", p.pref)
                p.addr = paddr
                p.tcp_port = tcp_port
                p.state = obj.get("state", p.state)
                p.cell_anticheat = bool(obj.get("cell_anticheat"))
            p.last_seen = time.time()

    def _handle_public_chat(self, obj):
        if str(obj.get("id", "")) == self.id:
            return
        name = str(obj.get("name", "Player"))
        text = str(obj.get("text", ""))[:500]
        self.add_chat(paint("[LAN %s] %s" % (name, text), "cyan"))

    def _handle_private_chat(self, obj):
        name = str(obj.get("from_name", "Player"))
        text = str(obj.get("text", ""))[:500]
        self.add_chat(paint("[PM from %s] %s" % (name, text), "cyan"))

    def _udp_listen_loop(self):
        while self.running:
            try:
                data, addr = self.udp_sock.recvfrom(4096)
            except OSError:
                break

            obj = parse_json_line(data.decode("utf-8", "replace"), key=self.lobby_key)
            if obj is None:
                continue

            if obj.get("app") != APP_ID:
                continue

            if obj.get("v") != LAN_VERSION:
                continue

            t = obj.get("type")
            if t == "beacon":
                self._handle_beacon(obj, addr)
            elif t == "lobby_chat":
                self._handle_public_chat(obj)

    def _beacon_loop(self):
        while self.running:
            self._clean_peers()
            self._clean_requests()

            obj = {
                "app": APP_ID,
                "v": LAN_VERSION,
                "type": "beacon",
                "id": self.id,
                "name": self.name,
                "pref": self.pref,
                "cell_anticheat": self.cell_anticheat,
                "state": "busy" if (self.in_match or self.pending_match) else "available",
                "udp_port": self.port,
                "tcp_port": self.tcp_port,
                "ts": time.time(),
            }

            obj = sign_obj(obj, self.lobby_key)
            payload = json.dumps(obj).encode("utf-8")

            for port in range(self.base_port, self.base_port + LAN_PORT_RANGE):
                try:
                    self.udp_sock.sendto(payload, ("<broadcast>", port))
                except OSError:
                    pass

            time.sleep(2.0)

    def _tcp_accept_loop(self):
        while self.running:
            try:
                conn, addr = self.tcp_sock.accept()
            except OSError:
                break

            if not self._allow_tcp(addr):
                try:
                    conn.close()
                except OSError:
                    pass
                continue

            with self.handshake_lock:
                if self.handshake_threads >= self.max_handshake_threads:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    continue
                self.handshake_threads += 1

            threading.Thread(
                target=self._tcp_conn_wrapper,
                args=(conn, addr),
                daemon=True
            ).start()

    def _handle_tcp_conn(self, conn, addr):
        keep_open = False
        reader = None

        try:
            conn.settimeout(5.0)
            reader = conn.makefile("r", encoding="utf-8")

            try:
                line = reader.readline(MAX_NET_LINE)
            except (socket.timeout, OSError):
                line = ""

            if not line or not line.endswith("\n"):
                line = ""

            obj = parse_json_line(line, key=self.lobby_key)

            if not isinstance(obj, dict):
                obj = None

            if obj is not None and obj.get("app") != APP_ID:
                obj = None

            if obj:
                t = obj.get("type")

                if t == "lobby_chat":
                    self._handle_private_chat(obj)

                elif t == "request":
                    keep_open = self._handle_incoming_request(conn, obj, addr, reader)

                elif t == "ping":
                    send_json_obj(conn, {"app": APP_ID, "type": "pong"})

        except Exception:
            pass

        finally:
            if not keep_open:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle_incoming_request(self, conn, obj, addr, reader=None):
        if self.in_match or self.pending_match:
            try:
                send_json_obj(conn, {"type": "reject", "reason": "busy"})
            except OSError:
                pass
            return False

        with self.request_lock:
            if len(self.incoming_requests) >= self.max_incoming_requests:
                try:
                    send_json_obj(conn, {"type": "reject", "reason": "busy"})
                except OSError:
                    pass
                return False

        rid = str(obj.get("request_id") or secrets.token_hex(4))
        from_id = str(obj.get("from_id", "?"))

        req = {
            "request_id": rid,
            "sock": conn,
            "reader": reader,
            "from_id": from_id,
            "from_name": str(obj.get("from_name", "Player")),
            "pref": obj.get("pref", "single"),
            "nonce": str(obj.get("nonce", "")),
            "cell_anticheat": bool(obj.get("cell_anticheat")),
            "time": time.time(),
            "addr": addr,
        }

        with self.request_lock:
            for old_rid, old in list(self.incoming_requests.items()):
                if old["from_id"] == from_id:
                    if old.get("reader") is not None:
                        try:
                            old["reader"].close()
                        except Exception:
                            pass
                    try:
                        old["sock"].close()
                    except OSError:
                        pass
                    del self.incoming_requests[old_rid]

            self.incoming_requests[rid] = req

        self.print_now(paint(
            "Incoming LAN match request from %s. Type requests." % req["from_name"],
            "yellow"
        ))
        return True

    def list_peers(self):
        self._clean_peers()
        with self.peer_lock:
            peers = sorted(self.peers.values(), key=lambda p: (p.name.lower(), p.id))

        self.last_listing = {}
        if not peers:
            self.print_now("No available LAN players found.")
            return

        for i, p in enumerate(peers, 1):
            self.last_listing[i] = p
            pref = "Salvo" if p.pref == "salvo" else "Normal"
            state = "busy" if p.state != "available" else "available"
            cell = "cell" if getattr(p, "cell_anticheat", False) else "classic"
            self.print_now("%d) %-16s pref=%-6s anti=%-7s state=%s addr=%s:%d"
                           % (i, p.name, pref, cell, state, p.addr[0], p.addr[1]))

    def list_requests(self):
        self._clean_requests()
        with self.request_lock:
            reqs = sorted(self.incoming_requests.items(), key=lambda kv: kv[1]["from_name"].lower())

        self.last_req_listing = {}
        if not reqs:
            self.print_now("No incoming match requests.")
            return

        for i, (rid, req) in enumerate(reqs, 1):
            self.last_req_listing[i] = rid
            pref = "Salvo" if req["pref"] == "salvo" else "Normal"
            self.print_now("%d) %-16s pref=%s" % (i, req["from_name"], pref))

    def resolve_peer(self, token):
        token = token.strip()

        if token.isdigit():
            return self.last_listing.get(int(token))

        m = re.match(r"^(\d+\.\d+\.\d+\.\d+)(?::(\d+))?$", token)
        if m:
            ip = m.group(1)
            port = int(m.group(2)) if m.group(2) else self.port
            pid = "manual:%s:%d" % (ip, port)
            return PeerInfo(pid, ip, self.pref, (ip, port), port, "available", self.cell_anticheat)

        return None

    def cmd_request(self, arg):
        if not arg:
            self.print_now("Usage: request <#|IP[:port]>")
            return

        peer = self.resolve_peer(arg)
        if peer is None:
            self.print_now("No such player. Use list first, or request IP[:port].")
            return

        self.send_request_to_peer(peer)

    def cmd_accept(self, arg):
        if not arg.isdigit():
            self.print_now("Usage: accept <#>  (see requests)")
            return

        rid = self.last_req_listing.get(int(arg))
        if not rid:
            self.print_now("No such incoming request. Type requests.")
            return

        self.accept_incoming(rid)

    def cmd_reject(self, arg):
        if not arg.isdigit():
            self.print_now("Usage: reject <#>  (see requests)")
            return

        rid = self.last_req_listing.get(int(arg))
        if not rid:
            self.print_now("No such incoming request. Type requests.")
            return

        with self.request_lock:
            req = self.incoming_requests.pop(rid, None)

        if req:
            try:
                send_json_obj(req["sock"], {"type": "reject", "reason": "rejected"})
            except OSError:
                pass

            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass

            try:
                req["sock"].close()
            except OSError:
                pass

            self.print_now("Rejected request from %s." % req["from_name"])

    def cmd_cancel(self):
        if self.pending_match:
            self.cancel_pending()
        elif self.outgoing_request:
            self.cancel_outgoing()
        else:
            self.print_now("Nothing to cancel.")

    def cmd_tell(self, arg):
        parts = arg.split(None, 1)
        if len(parts) < 2:
            self.print_now("Usage: tell <#|IP[:port]> <message>")
            return

        peer = self.resolve_peer(parts[0])
        if peer is None:
            self.print_now("No such player.")
            return

        self.send_private_chat(peer, parts[1])

    def send_public_chat(self, text):
        text = text.strip()[:500]
        if not text:
            return

        obj = {
            "app": APP_ID,
            "v": LAN_VERSION,
            "type": "lobby_chat",
            "id": self.id,
            "name": self.name,
            "text": text,
            "ts": time.time(),
        }

        obj = sign_obj(obj, self.lobby_key)
        payload = json.dumps(obj).encode("utf-8")

        sent = False

        try:
            self.udp_sock.sendto(payload, ("<broadcast>", self.port))
            sent = True
        except OSError:
            sent = False

        if not sent:
            with self.peer_lock:
                peers = list(self.peers.values())

            for p in peers:
                try:
                    self.udp_sock.sendto(payload, p.addr)
                    sent = True
                except OSError:
                    pass

        if not sent:
            self.print_now("Could not send chat. Broadcast may be blocked by firewall/network policy.")
            return

        self.add_chat(paint("[You] %s" % text, "cyan"))

    def send_private_chat(self, peer, text):
        text = text.strip()[:500]
        if not text:
            return

        obj = {
            "app": APP_ID,
            "type": "lobby_chat",
            "scope": "private",
            "from_id": self.id,
            "from_name": self.name,
            "text": text,
        }

        try:
            s = socket.create_connection(peer.addr, timeout=3.0)
            send_json_obj(s, obj, key=self.lobby_key)
            s.close()
            self.add_chat(paint("[You -> %s] %s" % (peer.name, text), "cyan"))
        except OSError as e:
            self.print_now("Could not send private chat: %s" % e)

    def send_request_to_peer(self, peer):
        if self.in_match or self.pending_match:
            self.print_now("You are already in or entering a match.")
            return

        with self.out_lock:
            if self.outgoing_request:
                self.print_now("You already have an outgoing request. Use cancel first.")
                return

        rid = secrets.token_hex(4)
        nonce = secrets.token_hex(16)

        try:
            sock = socket.create_connection(peer.addr, timeout=5.0)
        except OSError as e:
            self.print_now("Could not connect to %s: %s" % (peer.name, e))
            return

        msg = {
            "app": APP_ID,
            "type": "request",
            "request_id": rid,
            "from_id": self.id,
            "from_name": self.name,
            "pref": self.pref,
            "cell_anticheat": self.cell_anticheat,
            "nonce": nonce,
        }

        try:
            send_json_obj(sock, msg, key=self.lobby_key)
        except OSError as e:
            sock.close()
            self.print_now("Could not send request: %s" % e)
            return

        req = {
            "request_id": rid,
            "peer_id": peer.id,
            "peer_name": peer.name,
            "sock": sock,
            "nonce": nonce,
            "time": time.time(),
            "accepted": False,
            "cancelled": False,
        }

        with self.out_lock:
            self.outgoing_request = req

        threading.Thread(target=self._outgoing_request_waiter, args=(req,), daemon=True).start()
        self.print_now("Sent match request to %s." % peer.name)

    def cancel_outgoing(self):
        with self.out_lock:
            req = self.outgoing_request
            self.outgoing_request = None

        if req:
            req["cancelled"] = True
            try:
                req["sock"].close()
            except OSError:
                pass
            self.print_now("Outgoing request cancelled.")

    def _clear_outgoing_if(self, req):
        with self.out_lock:
            if self.outgoing_request is req:
                self.outgoing_request = None

    def _outgoing_request_waiter(self, req):
        reader = None

        try:
            req["sock"].settimeout(REQUEST_TIMEOUT + 5.0)
            reader = req["sock"].makefile("r", encoding="utf-8")

            try:
                line = reader.readline(MAX_NET_LINE)
            except (socket.timeout, OSError):
                line = ""

            if not line or not line.endswith("\n"):
                line = ""

            if req.get("cancelled"):
                return

            obj = parse_json_line(line, key=self.lobby_key)
            if obj is None:
                self.print_now("No valid reply from %s." % req["peer_name"])
                return

            if obj.get("type") == "accept":
                req["accepted"] = True
                self._start_match_from_outgoing(req, obj, reader)

            elif obj.get("type") == "reject":
                self.print_now("%s rejected your request (%s)."
                               % (req["peer_name"], obj.get("reason", "")))

            else:
                self.print_now("Unexpected reply from %s." % req["peer_name"])

        except Exception:
            if not req.get("cancelled") and not req.get("accepted"):
                self.print_now("Connection to %s closed." % req["peer_name"])

        finally:
            if not req.get("accepted"):
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        pass
                try:
                    req["sock"].close()
                except OSError:
                    pass
                self._clear_outgoing_if(req)

    def _start_match_from_outgoing(self, req, obj, reader=None):
        if self.pending_match or self.match or self.in_match:
            try:
                send_json_obj(req["sock"], {"type": "abort", "reason": "busy"})
            except OSError:
                pass

            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass

            try:
                req["sock"].close()
            except OSError:
                pass
            return

        acceptor_id = str(obj.get("from_id", req["peer_id"]))
        acceptor_name = str(obj.get("from_name", req["peer_name"]))
        accept_pref = obj.get("pref", "single")
        accept_nonce = str(obj.get("nonce", ""))
        accept_cell = bool(obj.get("cell_anticheat"))

        mode, first_id, match_id, key, seed = derive_match_params(
            self.id, req["nonce"], self.pref,
            acceptor_id, accept_nonce, accept_pref,
            extra_key=self.lobby_key
        )

        match = MatchConnection(
            self, req["sock"], acceptor_id, acceptor_name,
            mode, first_id, self.id, match_id, key,
            reader=reader
        )

        match.cell_anticheat = self.cell_anticheat and accept_cell
        match.async_handler = lambda evt: self.print_now("Pending match connection lost.")
        match.start()

        self._clear_outgoing_if(req)
        self.pending_match = match

        self.print_now(paint(
            "MATCH FOUND with %s! Mode: %s. Use Start Match in the LAN menu."
            % (acceptor_name, "Salvo" if mode == "salvo" else "Normal"),
            "green", "bold"
        ))

    def accept_incoming(self, rid):
        with self.request_lock:
            req = self.incoming_requests.pop(rid, None)

        if not req:
            self.print_now("No such incoming request.")
            return

        if self.in_match or self.pending_match:
            try:
                send_json_obj(req["sock"], {"type": "reject", "reason": "busy"})
            except OSError:
                pass
            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass
            try:
                req["sock"].close()
            except OSError:
                pass
            self.print_now("You are already in or entering a match.")
            return

        with self.out_lock:
            out = self.outgoing_request
        if out and out.get("peer_id") == req["from_id"]:
            self.cancel_outgoing()

        nonce = secrets.token_hex(16)
        msg = {
            "app": APP_ID,
            "type": "accept",
            "request_id": req["request_id"],
            "from_id": self.id,
            "from_name": self.name,
            "to_id": req["from_id"],
            "pref": self.pref,
            "cell_anticheat": self.cell_anticheat,
            "nonce": nonce,
        }

        try:
            send_json_obj(req["sock"], msg, key=self.lobby_key)
        except OSError as e:
            self.print_now("Could not accept request: %s" % e)
            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass
            try:
                req["sock"].close()
            except OSError:
                pass
            return

        mode, first_id, match_id, key, seed = derive_match_params(
            req["from_id"], req["nonce"], req["pref"],
            self.id, nonce, self.pref,
            extra_key=self.lobby_key
        )

        match = MatchConnection(
            self, req["sock"], req["from_id"], req["from_name"],
            mode, first_id, self.id, match_id, key,
            reader=req.get("reader")
        )

        match.cell_anticheat = self.cell_anticheat and bool(req.get("cell_anticheat"))
        self._run_match_object(match)

    def cancel_pending(self):
        match = self.pending_match
        self.pending_match = None

        if match:
            try:
                match.send({"type": "abort", "reason": "cancelled"})
            except Exception:
                pass
            match.close()
            self.print_now("Pending match cancelled.")

    def run_pending_match(self):
        match = self.pending_match
        self.pending_match = None

        if match is None:
            self.print_now("No pending match.")
            return

        if match.closed:
            self.print_now("Pending match connection is already closed.")
            return

        self._run_match_object(match)

    def _enter_match_pre(self, match):
        self.cancel_outgoing()

        with self.request_lock:
            reqs = list(self.incoming_requests.values())
            self.incoming_requests.clear()

        for req in reqs:
            try:
                send_json_obj(req["sock"], {"type": "reject", "reason": "busy"})
            except OSError:
                pass

            if req.get("reader") is not None:
                try:
                    req["reader"].close()
                except Exception:
                    pass

            try:
                req["sock"].close()
            except OSError:
                pass

        if self.pending_match is not None and self.pending_match is not match:
            try:
                self.pending_match.send({"type": "abort", "reason": "busy"})
            except Exception:
                pass
            self.pending_match.close()

        self.pending_match = None
        self.in_match = True
        self.match = match

    def _run_match_object(self, match):
        if match.closed:
            self.print_now("Match connection closed.")
            return

        self._enter_match_pre(match)

        if not match.started:
            match.start()

        result = "abandoned"
        game = None

        try:
            game = LANGame(self, match)
            result = game.run()
        except Quit:
            try:
                match.send({"type": "surrender"})
            except Exception:
                pass
            result = "loss"
        except Exception as e:
            self.print_now("LAN error: %s" % e)
            result = "abandoned"
        finally:
            match.close()
            self.match = None
            self.in_match = False

        if result == "win":
            self.lan_score["win"] += 1
            if game is not None and getattr(game, "win_kind", None) == "verified":
                self.lan_score["verified_win"] += 1
            else:
                self.lan_score["forfeit_win"] += 1
        elif result == "loss":
            self.lan_score["loss"] += 1

        self.print_now("LAN score: wins %d (verified %d, forfeit %d), losses %d"
                       % (self.lan_score["win"],
                          self.lan_score["verified_win"],
                          self.lan_score["forfeit_win"],
                          self.lan_score["loss"]))

    def handle_command(self, raw):
        parts = raw.strip().split(None, 1)
        if not parts:
            return

        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("help", "h", "?"):
            print(LAN_HELP)

        elif cmd == "name":
            name = arg.strip()
            if not name:
                self.print_now("Usage: name <name>")
            else:
                self.name = name[:24]
                self.print_now("Name set to %s." % self.name)

        elif cmd == "pref":
            p = arg.strip().lower()
            if p in ("normal", "single"):
                self.pref = "single"
                self.print_now("Preference set to Normal.")
            elif p == "salvo":
                self.pref = "salvo"
                self.print_now("Preference set to Salvo.")
            else:
                self.print_now("Usage: pref normal|salvo")

        elif cmd == "password":
            self.set_password(arg)

        elif cmd == "anticheat":
            a = arg.strip().lower()
            if a in ("cell", "on", "percell", "1"):
                self.cell_anticheat = True
                self.print_now("Per-cell commitment anti-cheat preference enabled.")
            elif a in ("off", "0"):
                self.cell_anticheat = False
                self.print_now("Per-cell commitment anti-cheat preference disabled.")
            else:
                self.print_now("Usage: anticheat cell | off")

        elif cmd == "status":
            self.print_now("Password lobby: %s" % ("enabled" if self.lobby_key else "disabled"))
            self.print_now("Per-cell anti-cheat preference: %s" %
                           ("enabled" if self.cell_anticheat else "disabled"))
            self.print_now("LAN score: wins %d (verified %d, forfeit %d), losses %d"
                           % (self.lan_score["win"],
                              self.lan_score["verified_win"],
                              self.lan_score["forfeit_win"],
                              self.lan_score["loss"]))

        elif cmd == "list":
            self.list_peers()

        elif cmd == "requests":
            self.list_requests()

        elif cmd == "request":
            self.cmd_request(arg)

        elif cmd == "accept":
            self.cmd_accept(arg)

        elif cmd == "reject":
            self.cmd_reject(arg)

        elif cmd == "cancel":
            self.cmd_cancel()

        elif cmd == "say":
            if arg.strip():
                self.send_public_chat(arg)
            else:
                self.print_now("Usage: say <message>")

        elif cmd == "tell":
            self.cmd_tell(arg)

        elif cmd == "chat":
            self.print_chat_history()

        elif cmd == "start":
            if self.pending_match:
                self.run_pending_match()
            else:
                self.print_now("No pending match.")

        elif cmd in ("quit", "exit", "back"):
            self.running = False

        else:
            self.print_now("Unknown command. Type help.")

    def notify(self, line):
        self.notifications.append(line)

    def _lobby_header(self):
        lines = list(title_banner())
        lines.append("")
        lines.append("  " + paint("LAN LOBBY", "bold"))

        with self.peer_lock:
            peer_count = len(self.peers)

        with self.request_lock:
            req_count = len(self.incoming_requests)

        status = [
            "Name: %s" % self.name,
            "Port: %d" % self.port,
            "Players seen: %d" % peer_count,
            "Incoming requests: %d" % req_count,
            "Mode preference: %s" % ("Salvo" if self.pref == "salvo" else "Normal"),
            "Password lobby: %s" % ("enabled" if self.lobby_key else "disabled"),
            "Cell anti-cheat: %s" % ("enabled" if self.cell_anticheat else "disabled"),
            "LAN score: %d W (%d verified, %d forfeit) / %d L" % (
                self.lan_score["win"],
                self.lan_score["verified_win"],
                self.lan_score["forfeit_win"],
                self.lan_score["loss"],
            ),
        ]

        if self.outgoing_request:
            status.append(paint("Outgoing request pending...", "cyan"))

        if self.pending_match:
            status.append(paint("Pending match: %s" % self.pending_match.peer_name,
                                "green", "bold"))

        lines.append("")
        lines.extend(boxed_panel(paint("STATUS", "bold"), status))

        notices = list(self.notifications)[-4:]
        if notices:
            lines.append("")
            lines.extend(boxed_panel(paint("NOTICES", "bold"), notices))

        return "\n".join(lines)

    def _lobby_options(self):
        options = []
        actions = []

        if self.pending_match:
            options.append(paint("START MATCH with %s" % self.pending_match.peer_name,
                                 "green", "bold"))
            actions.append("start")

        with self.request_lock:
            req_count = len(self.incoming_requests)

        if req_count:
            options.append(paint("Incoming requests (%d)" % req_count,
                                 "yellow", "bold"))
        else:
            options.append("Incoming requests")
        actions.append("requests")

        options.append("Players / send match requests")
        actions.append("players")

        options.append("Chat")
        actions.append("chat")

        options.append("Settings")
        actions.append("settings")

        options.append("Status / score")
        actions.append("status")

        options.append("Help")
        actions.append("help")

        options.append("Advanced command line")
        actions.append("command")

        options.append("Quit LAN lobby")
        actions.append("quit")

        return options, actions

    def _choose_peer(self, title):
        self._clean_peers()

        with self.peer_lock:
            peers = sorted(self.peers.values(), key=lambda p: (p.name.lower(), p.id))

        if not peers:
            self.notify("No LAN players found.")
            return None

        options = []
        for p in peers:
            options.append("%s (%s:%d)" % (p.name, p.addr[0], p.addr[1]))

        options.append("Back")

        idx = select_menu(title, options)

        if idx == len(peers):
            return None

        return peers[idx]

    def _players_menu(self):
        sel = 0
        while self.running:
            self._clean_peers()

            with self.peer_lock:
                peers = sorted(self.peers.values(), key=lambda p: (p.name.lower(), p.id))

            options = []
            actions = []

            for p in peers:
                pref = "Salvo" if p.pref == "salvo" else "Normal"
                anti = "cell" if p.cell_anticheat else "classic"
                state = p.state or "available"

                options.append("%-16s  pref=%-6s anti=%-7s state=%s"
                               % (p.name, pref, anti, state))
                actions.append(p)

            options.append("Refresh list")
            actions.append("refresh")

            options.append("Back")
            actions.append("back")

            header = paint("LAN PLAYERS", "bold") + "\n\nSelect a player to request a match or chat."

            idx = select_menu(header, options, start_idx=sel)
            sel = idx
            act = actions[idx]

            if act == "back":
                return

            if act == "refresh":
                continue

            self._peer_actions(act)

    def _peer_actions(self, peer):
        header = "%s\n\n%s\nAddress: %s:%d\nPreference: %s\nAnti-cheat: %s\nState: %s" % (
            paint("PLAYER", "bold"),
            peer.name,
            peer.addr[0],
            peer.addr[1],
            "Salvo" if peer.pref == "salvo" else "Normal",
            "cell" if peer.cell_anticheat else "classic",
            peer.state or "available",
        )

        options = [
            "Send match request",
            "Send private chat",
            "Back",
        ]

        idx = select_menu(header, options)

        if idx == 0:
            self.send_request_to_peer(peer)
        elif idx == 1:
            text = ask("Private message to %s > " % peer.name)
            self.send_private_chat(peer, text)

    def _requests_menu(self):
        sel = 0
        while self.running:
            self._clean_requests()

            with self.request_lock:
                reqs = sorted(self.incoming_requests.items(),
                              key=lambda kv: kv[1]["from_name"].lower())

            options = []
            actions = []

            for rid, req in reqs:
                pref = "Salvo" if req["pref"] == "salvo" else "Normal"
                anti = "cell" if req.get("cell_anticheat") else "classic"

                options.append("%-16s  pref=%s anti=%s"
                               % (req["from_name"], pref, anti))
                actions.append(rid)

            options.append("Refresh")
            actions.append("refresh")

            options.append("Back")
            actions.append("back")

            header = paint("INCOMING REQUESTS", "bold") + "\n\nSelect a request to accept or reject."

            idx = select_menu(header, options, start_idx=sel)
            sel = idx
            act = actions[idx]

            if act == "back":
                return

            if act == "refresh":
                continue

            self._request_actions(act)

    def _request_actions(self, rid):
        with self.request_lock:
            req = self.incoming_requests.get(rid)

        if not req:
            self.notify("That request is no longer available.")
            return

        header = "%s\n\nFrom: %s\nMode preference: %s\nAnti-cheat: %s" % (
            paint("MATCH REQUEST", "bold"),
            req["from_name"],
            "Salvo" if req["pref"] == "salvo" else "Normal",
            "cell" if req.get("cell_anticheat") else "classic",
        )

        options = [
            "Accept and start match",
            "Reject request",
            "Back",
        ]

        idx = select_menu(header, options)

        if idx == 0:
            self.interactive_lobby = False
            try:
                self.accept_incoming(rid)
            finally:
                self.interactive_lobby = True
        elif idx == 1:
            self._reject_request_id(rid)

    def _reject_request_id(self, rid):
        with self.request_lock:
            req = self.incoming_requests.pop(rid, None)

        if not req:
            self.notify("That request is no longer available.")
            return

        try:
            send_json_obj(req["sock"], {"type": "reject", "reason": "rejected"})
        except OSError:
            pass

        if req.get("reader") is not None:
            try:
                req["reader"].close()
            except Exception:
                pass

        try:
            req["sock"].close()
        except OSError:
            pass

        self.notify("Rejected request from %s." % req["from_name"])

    def _chat_menu(self):
        sel = 0
        while self.running:
            history = list(self.chat_history)[-16:]

            if history:
                hist = "\n".join(history)
            else:
                hist = "No chat messages yet."

            header = paint("LAN CHAT", "bold") + "\n\n" + hist

            options = [
                "Send public chat",
                "Send private chat",
                "Refresh",
                "Back",
            ]

            idx = select_menu(header, options, start_idx=sel)
            sel = idx

            if idx == 0:
                text = ask("Public chat > ")
                self.send_public_chat(text)
            elif idx == 1:
                peer = self._choose_peer(paint("PRIVATE CHAT", "bold"))
                if peer is not None:
                    text = ask("Private message to %s > " % peer.name)
                    self.send_private_chat(peer, text)
            elif idx == 3:
                return

    def _settings_menu(self):
        sel = 0
        while self.running:
            options = [
                "Change name (current: %s)" % self.name,
                "Mode preference: %s" % ("Salvo" if self.pref == "salvo" else "Normal"),
                "Password lobby: %s" % ("enabled" if self.lobby_key else "disabled"),
                "Per-cell anti-cheat: %s" % ("enabled" if self.cell_anticheat else "disabled"),
                "Back",
            ]

            idx = select_menu(paint("LAN SETTINGS", "bold"), options, start_idx=sel)
            sel = idx

            if idx == 0:
                name = ask("New name (blank keeps current) > ").strip()[:24]
                if name:
                    self.name = name
                    self.notify("Name set to %s." % self.name)

            elif idx == 1:
                self.pref = "salvo" if self.pref == "single" else "single"
                self.notify("Mode preference set to %s."
                            % ("Salvo" if self.pref == "salvo" else "Normal"))

            elif idx == 2:
                if self.lobby_key:
                    if confirm("Clear lobby password?"):
                        self.set_password("")
                else:
                    pw = ask("Set lobby password (blank cancels) > ")
                    if pw.strip():
                        self.set_password(pw)

            elif idx == 3:
                self.cell_anticheat = not self.cell_anticheat
                self.notify("Per-cell anti-cheat %s."
                            % ("enabled" if self.cell_anticheat else "disabled"))

            else:
                return

    def _status_menu(self):
        clear()
        print()
        print(paint("LAN STATUS", "bold"))
        print()

        with self.peer_lock:
            peer_count = len(self.peers)

        with self.request_lock:
            req_count = len(self.incoming_requests)

        print("  Name: %s" % self.name)
        print("  Port: %d" % self.port)
        print("  Players seen: %d" % peer_count)
        print("  Incoming requests: %d" % req_count)
        print("  Mode preference: %s" % ("Salvo" if self.pref == "salvo" else "Normal"))
        print("  Password lobby: %s" % ("enabled" if self.lobby_key else "disabled"))
        print("  Per-cell anti-cheat: %s" % ("enabled" if self.cell_anticheat else "disabled"))
        print()
        print("  LAN score:")
        print("    Wins: %d" % self.lan_score["win"])
        print("    Verified wins: %d" % self.lan_score["verified_win"])
        print("    Forfeit wins: %d" % self.lan_score["forfeit_win"])
        print("    Losses: %d" % self.lan_score["loss"])
        print()

        if self.pending_match:
            print("  " + paint("Pending match: %s" % self.pending_match.peer_name,
                               "green", "bold"))
        else:
            print("  No pending match.")

        if self.outgoing_request:
            print("  " + paint("Outgoing request pending...", "cyan"))
        else:
            print("  No outgoing request pending.")

        print()
        ask("Press Enter to return > ")

    def _help_menu(self):
        clear()
        print()
        print(paint("LAN HELP", "bold"))
        print(LAN_HELP)
        ask("Press Enter to return > ")

    def _command_menu(self):
        self.interactive_lobby = False

        try:
            print()
            print("Advanced command mode. Type back to return.")

            while self.running:
                if self.pending_match is not None:
                    print(paint("Match ready with %s. Press Enter to start, or type cancel."
                                % self.pending_match.peer_name, "green"))

                raw = ask("LAN command > ")
                txt = raw.strip().lower()

                if txt in ("back", "return"):
                    return

                if self.pending_match is not None and txt in ("", "start"):
                    self.run_pending_match()
                    return

                if not raw.strip():
                    continue

                self.handle_command(raw)

        except Quit:
            return

        finally:
            self.interactive_lobby = True

    def run_lobby(self):
        try:
            self.start()
        except OSError as e:
            self.print_now("Cannot start LAN lobby: %s" % e)
            return

        self.interactive_lobby = True
        sel_action = None

        try:
            while self.running:
                self._clean_requests()

                if self.pending_match is not None and self.pending_match.closed:
                    self.pending_match = None

                header = self._lobby_header()
                options, actions = self._lobby_options()

                start_idx = actions.index(sel_action) if sel_action in actions else 0
                idx = select_menu(header, options, start_idx=start_idx)
                action = actions[idx]
                sel_action = action

                if action == "quit":
                    break

                elif action == "start":
                    if self.pending_match:
                        self.interactive_lobby = False
                        try:
                            self.run_pending_match()
                        finally:
                            self.interactive_lobby = True

                elif action == "players":
                    self._players_menu()

                elif action == "requests":
                    self._requests_menu()

                elif action == "chat":
                    self._chat_menu()

                elif action == "settings":
                    self._settings_menu()

                elif action == "status":
                    self._status_menu()

                elif action == "help":
                    self._help_menu()

                elif action == "command":
                    self._command_menu()

        except Quit:
            pass

        finally:
            self.interactive_lobby = False
            self.stop()
            time.sleep(0.1)


# ----------------------------------------------------------------------------
# Menus
# ----------------------------------------------------------------------------

def choose_level():  # type: ignore[no-untyped-def]
    options = ["%-7s  %s" % (name, desc) for name, _, desc in LEVELS]
    idx = select_menu(paint("DIFFICULTY", "bold"), options,
                      start_idx=_MENU_STATE.last_level)
    _MENU_STATE.last_level = idx
    return LEVELS[idx]


def choose_setup():  # type: ignore[no-untyped-def]
    options = [p[0] for p in SETUP_PRESETS]
    idx = select_menu(paint("GAME SETUP", "bold"), options,
                      start_idx=_MENU_STATE.last_game_setup)
    _MENU_STATE.last_game_setup = idx
    _, size, fleet_name = SETUP_PRESETS[idx]
    configure_board(size, fleet_name)


def choose_mode():  # type: ignore[no-untyped-def]
    idx = select_menu(
        paint("MODE", "bold"),
        ["Normal — one shot per turn", "Salvo — one shot per ship afloat"],
        start_idx=_MENU_STATE.last_mode,
    )
    _MENU_STATE.last_mode = idx
    return "single" if idx == 0 else "salvo"


def choose_tactic():  # type: ignore[no-untyped-def]
    idx = select_menu(
        paint("ENEMY TACTIC", "bold"),
        ["Random placement — ships anywhere",
         "Contrarian — ships hide in low-probability cells"],
        start_idx=_MENU_STATE.last_tactic,
    )
    _MENU_STATE.last_tactic = idx
    return idx == 1


def main():
    global USE_COLOR, ANSI_OK

    parser = argparse.ArgumentParser(description="Battleships vs. computer / LAN")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--bench", type=int, metavar="N", default=0,
                        help="headless AI stats: N games per level")
    parser.add_argument("--seed", type=int, default=0, help="seed for --bench")
    parser.add_argument("--include-nightmare", action="store_true",
                        help="include Nightmare in --bench (slow)")
    parser.add_argument("--load", metavar="FILE", default=None, help="resume a saved game")
    parser.add_argument("--lan-port", type=int, default=DEFAULT_LAN_PORT,
                        help="UDP/TCP port for LAN matchmaking")
    parser.add_argument("--lan-password", default=None,
                        help="optional LAN lobby password")
    parser.add_argument("--board", type=int, default=None,
                        help="board size (6-14); skips the setup menu")
    parser.add_argument("--fleet", choices=sorted(FLEET_PRESETS.keys()), default=None,
                        help="fleet preset; skips the setup menu")
    parser.add_argument("--contrarian", action="store_true",
                        help="enemy ships hide in low-probability cells")
    parser.add_argument("--campaign", action="store_true",
                        help="start campaign mode directly")
    args = parser.parse_args()

    if args.bench:
        import statistics
        levels = LEVELS if args.include_nightmare else [lv for lv in LEVELS if lv[0] != "Nightmare"]
        print("Bench: %d game(s) per level, seed %d" % (args.bench, args.seed))

        for name, cls, _ in levels:
            shots = bench_solo(cls, args.bench, args.seed)
            print("  %-7s median %5.1f  shots %s" % (name, statistics.median(shots), shots))

        print("Round-robin (one game per pair):")
        wins = dict((name, 0) for name, _, _ in levels)

        for i in range(len(levels)):
            for j in range(i + 1, len(levels)):
                w, na, nb = bench_match(levels[i][1], levels[j][1], "%s-%d-%d" % (args.seed, i, j))
                victor = levels[i][0] if w == 0 else levels[j][0]
                wins[victor] += 1
                print("  %s vs %s -> %s wins (%d-%d)" % (levels[i][0], levels[j][0], victor, na, nb))

        print("Wins: %s" % ", ".join("%s %d" % (n, wins[n]) for n, _, _ in levels))
        return

    set_terminal_state(sys.stdout.isatty() and "NO_COLOR" not in os.environ and not args.no_color,
                       sys.stdout.isatty())
    if os.name == "nt":
        os.system("")

    # CLI overrides
    if args.board is not None or args.fleet is not None:
        try:
            configure_board(args.board or 10, args.fleet or "classic")
        except ValueError as e:
            print("Setup error: %s" % e)
            configure_board(10, "classic")

    use_alt = sys.stdout.isatty()
    if use_alt:
        print("\033[?1049h", end="")

    score = {"win": 0, "loss": 0}
    lan_score = {"win": 0, "loss": 0, "verified_win": 0, "forfeit_win": 0}

    try:
        if args.load:
            try:
                game = load_game(args.load)
            except ValueError as e:
                print("Cannot load %s: %s" % (args.load, e))
                return

            result = game.run()
            if result in score:
                score[result] += 1
            print("Session score: you %d - %d computer" % (score["win"], score["loss"]))
            return

        if args.campaign:
            if args.board is None and args.fleet is None:
                choose_setup()
            mode = "single"
            camp = CampaignGame(mode=mode, contrarian=args.contrarian)
            camp.run()
            return

        menu_sel = 0
        while True:
            banner = title_banner()
            header_lines = list(banner)
            header_lines.append("")
            header_lines.append("  " + paint("MAIN MENU", "bold"))
            header_lines.append("")
            header_lines.append(
                "  vs AI: %d-%d   ·   LAN: %d W (%d verified, %d forfeit) / %d L   ·   board %d×%d, %s"
                % (score["win"], score["loss"],
                   lan_score["win"], lan_score["verified_win"],
                   lan_score["forfeit_win"], lan_score["loss"],
                   SIZE, SIZE,
                   next((name for name, s, fn in SETUP_PRESETS
                         if s == SIZE and FLEET_PRESETS[fn] == FLEET), "custom"))
            )
            header = "\n".join("  " + line if not line.startswith("  ") else line
                               for line in header_lines)

            menu_opts = [
                "New game (vs computer)",
                "Campaign (Easy → Nightmare)",
                "Hotseat (2 players)",
                "LAN Matchmaking",
                "How to play",
                "Visual Settings",
                "Quit",
            ]

            choice = select_menu(header, menu_opts, allow_quit=True, start_idx=menu_sel)
            if choice != -1:
                menu_sel = choice

            if choice == -1 or choice == 6:
                break

            # --- New game -------------------------------------------------
            if choice == 0:
                if args.board is None and args.fleet is None:
                    choose_setup()
                name, cls, _ = choose_level()
                mode = choose_mode()
                contrarian = args.contrarian or choose_tactic()

                while True:
                    game = Game(name, cls, mode=mode, contrarian=contrarian)
                    result = game.run()
                    if result in score:
                        score[result] += 1

                    if result in ("win", "loss"):
                        print()
                        if confirm("Review your shots?"):
                            show_shot_review(game)

                    print()
                    print("Session score: you %d - %d computer" % (score["win"], score["loss"]))
                    if confirm("Play again on %s?" % name):
                        continue
                    break
                continue

            # --- Campaign -------------------------------------------------
            if choice == 1:
                if args.board is None and args.fleet is None:
                    choose_setup()
                mode = choose_mode()
                contrarian = args.contrarian or choose_tactic()

                camp = CampaignGame(mode=mode, contrarian=contrarian)
                result = camp.run()
                if result in score:
                    score[result] += 1
                print()
                print("Session score: you %d - %d computer" % (score["win"], score["loss"]))
                continue

            # --- Hotseat --------------------------------------------------
            if choice == 2:
                configure_board(10, "classic")
                HotseatGame().run()
                continue

            # --- LAN ------------------------------------------------------
            if choice == 3:
                configure_board(10, "classic")
                client = LANClient(port=args.lan_port)
                if args.lan_password:
                    client.set_password(args.lan_password)
                client.run_lobby()
                for k, v in client.lan_score.items():
                    lan_score[k] += v
                continue

            # --- How to play ---------------------------------------------
            if choice == 5: visual_settings_menu(); continue
            if choice == 4:
                if supports_cursor_ui():
                    clear()
                    print()
                    for line in title_banner():
                        print("  " + line)
                    print()
                    print(HOW_TO_PLAY)
                    with KeyReader() as kr:
                        print("  " + paint("Press any key to return...", "grey"))
                        kr.get_key()
                else:
                    print(HOW_TO_PLAY)
                continue

    except Quit:
        pass
    finally:
        if use_alt:
            print("\033[?1049l", end="")
        print("Fair winds. Goodbye.")


if __name__ == "__main__":
    main()
