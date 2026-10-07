# Epic Game Feel — Design Spec
Date: 2026-10-07
Status: approved in-chat, pending spec review
Constraint: single-file `battleships.py`, zero-dependency, TTY-first with typed fallback.

## 1. Intent
Make every shot land. Snappy by default (~0.3s hit, ~1.0s sink), full Hollywood opt-in via Visual Settings. No rules changes.

## 2. Architecture
- Single owner stays `_VISUAL_SETTINGS` (`battleships.py:410`). `VISUAL` dict remains compat view.
- All new effects gated by `can_animate()` (`battleships.py:417`) + per-flag `vis(name)`. Non-TTY / `--no-color` falls back to current `print` behavior. No-ops on exception (same pattern as `_burst_frames`).
- New `VISUAL_DEFAULTS` keys (all bool):
  - `screen_shake: True`, `hit_stop: True`, `kill_cam: True`
  - `damage_fire: True`, `sonar_sweep: True`, `captain_taunts: True`, `streaks: True`
  - `epic_mode: False` (master: 1.5–2.0s timings + enables `screen_flash` + `terminal_bell` patterns for the session only)
- Timings table:
  - default: hit-stop 0.12s, sunk-stop 0.25s, burst hold hit 0.30s / sunk 0.45s (existing), shake 3 frames / 0.09s total
  - epic_mode: hit-stop 0.30s, sunk-stop 0.80s, burst hold hit 0.8s / sunk 1.5s, shake 6 frames / 0.30s

## 3. Components
### 3.1 Impact core (touch `_burst_frames:447`, `burst_shot:759`, `show_sunk_reveal:503`)
- `screen_shake(cells, intensity)`: re-print last `render_boards` with random ±1 col jitter for N frames. Intensity 1 default, 2 on sunk, 3 in epic_mode. Skipped if board width would wrap.
- `hit_stop(sunk: bool)`: `time.sleep(0.12 or 0.25 / epic 0.30 or 0.80)` only when `vis("hit_stop")`.
- `kill_cam(player, enemy, ship_name, last_player)`: extends `show_sunk_reveal`. Letter-by-letter `S → S U → S U N K` (0.06s/letter, 0.15s epic), then obituary line e.g. `enemy Cruiser destroyed at C4 — 3 shots to kill`. Reuses `big_banner`. V1 runs to completion (no input polling); max added latency capped by timings table (§2).

### 3.2 Persistent battle state (touch `render_boards`, `fleet_panel:674`, `water_char:487`, `Spinner:693`)
- `damage_fire`: any unsunk ship with ≥1 hit renders hit cells as flickering `*`/`!` (red/yellow alternate by `int(time.time()*4)%2`). Fleet panel appends `🔥`→ ASCII `*` marker (`*` to stay zero-dep safe, no emoji). Pure render change, no state change.
- `sonar_sweep`: on enemy turn, `Spinner.__enter__` already emits `ENEMY TURN` banner; add 4-frame `((.)) → ((o)) → (((o)))` ping line before spinner starts, 0.28s total. Reuses `burst_banner` path.
- `heartbeat`: in `burst_shot`, after sunk, emit two short `_bell("hit")` pulses spaced by `max(0.3, 1.2 - 0.15*sunk_count)` seconds total (sunk_count from `Board.sunk_count`). Only if `vis("terminal_bell")` or `epic_mode`. Inline sleeps, never blocks input beyond the sunk-stop in §2.

### 3.3 Personality + streaks (touch `GameStats:144`, `burst_shot`, end screens)
- `TAUNTS: Dict[level, Dict[event, List[str]]]`: 10 lines per AI level (Easy/Medium/Hard/Expert/Nightmare) × events (player_hit, player_miss, player_sunk, losing). Example tone: Easy cocky, Nightmare cold. Shown as `ENEMY CAPTAIN: "..."` via `_burst_print` 0.9s. Max 1 per turn, 35% probability to avoid spam. `vis("captain_taunts")` gates.
- `streaks`: extend `GameStats` with `streak: int`. Increment on player hit, reset on miss. Announce `DOUBLE HIT!` at 2, `ON FIRE! xN` at 3+. Banner only, no score change (YAGNI: no persistent rank in this spec).
- War report: extend `par_line:1631` output with ASCII sparkline of hit/miss sequence (`█` hit, `·` miss, last 20 shots) + `MVP: C4 (sunk Cruiser in 3)`. Data already in `board.shots` + `order`; no new persistence.

## 4. Data flow
`fire() -> burst_shot() -> [hit_stop -> screen_shake -> _burst_frames -> taunt/streak -> kill_cam if sunk -> heartbeat]` — all fire-and-forget renders. Game state (`Board`, `Knowledge`, `GameStats`) unchanged except `streak` counter. No LAN protocol change; effects are local-only. LAN reuses `burst_shot_lan:805` with same helpers.

## 5. Error handling / fallback
- Every effect wrapped in `try/except: pass` like existing visuals.
- If `not supports_cursor_ui()` or `not can_animate()`: skip sleeps/shake/sweep, keep text lines.
- Epic timings never exceed 2.0s single pause; kill-cam runs to completion in V1 (capped by §2 table).
- No new threads except existing `Spinner`; shake/sweep run inline to avoid race with board redraw.

## 6. Visual Settings menu (`visual_settings_menu:532`)
Append 8 rows: 7 flags + `EPIC MODE (all Hollywood, slower)`. `Reset to defaults` restores them. Help line: `Snappy default. EPIC MODE is slower but juicier.`

## 7. Testing
- `python3 -m py_compile battleships.py`
- `python3 battleships.py --bench 5 --seed 0` (no perf regression; visuals disabled headless)
- Manual TTY: solo Normal 1 game — verify hit shake, sunk kill-cam, fire flicker, 1 taunt, streak callout, Settings toggle off kills all new effects.
- Non-TTY: `echo "B7" | python3 battleships.py --no-color` — no crash, no sleeps >0.05s.
- LAN: no protocol diff; `burst_shot_lan` smoke test via hotseat.

## 8. Out of scope (YAGNI)
No real audio libs, no emoji dependency, no persistent ranks/scores, no weather/fog boards, no campaign story. Those are B/C-full follow-ups.
