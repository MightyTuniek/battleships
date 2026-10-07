# Battleships — Codebase Analysis

> Sources: `battleships.py` (7215 lines, single file, stdlib only), `README.md`, `LICENSE` (MIT, (c) 2026 MightyTuniek).
> All `battleships.py:NNN` citations refer to the file as measured at analysis time (`wc -l battleships.py` = 7215, `py_compile` clean).

## 1. Overall architecture, size, data flow

- **Single-file, zero-dependency terminal game.** Imports are stdlib only: `argparse, math, os, random, re, sys, socket, threading, json, time, hashlib, hmac, queue, secrets, collections`, `dataclasses`, `typing` (`battleships.py:16-37`). Optional `readline`, platform `msvcrt`/`termios`/`tty`/`select` imported lazily inside `KeyReader`/`Spinner`.
- **Scale:** ~7215 lines, ~31 `class` definitions, ~349 `def`s (AST count). Top-level sections in order: typing contracts → config/state dataclasses → terminal/visual → box drawing → input/menus → coordinates → placement cache → `Board` → `Knowledge` → density/probability → benchmark/save → AI → rendering → placement UI → `Game` → `CampaignGame` → `HotseatGame` → LAN crypto/helpers → `MatchConnection` → `LANGame` → `PeerInfo`/`LANClient` (~1600 lines) → menus/`main()`.
- **Explicit contracts:** `Cell = Tuple[int,int]`, `ShipCells`, `ShotResult`, `Shooter`/`BoardLike` Protocols (`battleships.py:42-55`).
- **Central config/state (replaces globals, but globals kept for compat):**
  - `BoardConfig` (`battleships.py:59-77`) + singleton `_CONFIG` + mutable globals `SIZE/COLS/FLEET/SHIP_LEN/FLEET_LENGTHS` (`battleships.py:295-299`). Sole writer is `configure_board(size, fleet_name)` (`battleships.py:322-340`), which clamps 6–14, validates longest ship fits, and calls `_rebuild_placements()`.
  - `VisualSettings` (`battleships.py:81-100`) owning `VISUAL` dict view (`battleships.py:419-420`); `MenuState` (`battleships.py:104-113`); `TerminalState` + `set_terminal_state()` (`battleships.py:117-140`); `GameStats` typed wrapper (`battleships.py:144-169`).
  - `PlacementCache` (`battleships.py:172-205`) precomputes every legal placement per ship length as `(cells_tuple, frozenset, horizontal)`; module `PLACEMENTS` is a compat view. Rebuilt on every `configure_board`.
  - LAN splits: `PeerRegistry`, `ChatLog` (200-msg cap), `TcpRateLimiter` (10 hits / 5 s), `DensityEngine` facade (`battleships.py:208-284`).
- **Core data flow (solo):**
  `Game.__init__` creates `player: Board`, `enemy: Board`, `ai: AI(Knowledge)`, `pk: Knowledge`, `stats`, `turn` (`battleships.py:2615-2639`) → `Game.setup()` places player fleet (`battleships.py:2643-2737`) → `Game.run()` loop: `get_shot()` → `Board.fire()` → `Knowledge.record()` → `burst_shot()` effects → enemy `AI.choose()/record()` → `finish()` (`battleships.py:2972-3126`).
- **`Board` is ground truth** (`battleships.py:1318-1402`): `cells[r][c] -> ship|None`, `ship_cells`, `shots[pos]->bool`, `order`. `fire()` asserts no double-fire (`battleships.py:1374-1378`).
- **`Knowledge` is belief state** (`battleships.py:1408-1532`): `remaining` lengths, `miss/hit/sunk`, `_hit_time`, `_sinkings`. `record()` + `_refresh()`/`_assignments()`/`_leftover_ok()` disambiguate sunk vs. still-afloat hits via constraint search. `Knowledge.from_board()` (`battleships.py:1439-1460`) reconstructs belief from a finished board (used by save-resume and par).
- **Entry:** `main()` (`battleships.py:7006-7215`): `argparse` → `--bench` headless path → terminal init → alt-screen (`\033[?1049h`) → menu loop (New game / Campaign / Hotseat / LAN / How to play / Visual / Quit).

## 2. Game modes, rules, board/fleet presets, controls

- **Modes** (`README.md:69-98`, `battleships.py:2612-3520`):
  - **Solo vs AI:** `Game` (`battleships.py:2612-3126`). Flow in `main()`: setup → difficulty → mode → tactic (`battleships.py:7132-7155`, `choose_level/mode/tactic` at `battleships.py:6968-7003`). Player fires first; first to sink entire enemy fleet wins.
  - **Campaign:** `CampaignGame` (`battleships.py:3132-3239`). Iterates `LEVELS` Easy→Nightmare, reuses first mission's player fleet (`reuse_fleet`), one loss ends run, per-mission + totals summary. Direct via `--campaign` (`battleships.py:7087-7093`).
  - **Hotseat:** `HotseatGame` (`battleships.py:3249-3472`). Two `Board`+`Knowledge` pairs, handoff screen (`handoff()` at `battleships.py:3268-3286`), fixed 10×10 classic (enforced in `main()` at `battleships.py:7173-7176`).
  - **LAN:** `LANGame` + `LANClient` (see §4). Forced 10×10 classic on entry (`battleships.py:7179-7180`).
- **Rules:**
  - `Normal/single`: 1 shot/turn (`battleships.py:3038-3076`).
  - `Salvo`: shots/turn = own afloat ship count. Helpers `salvo_size()`, `salvo_has_duplicate()`, `apply_salvo()` (`battleships.py:1927-1946`); solo loop `Game._take_player_salvo/_enemy_salvo_response` (`battleships.py:2934-2959`, `2988-3036`); LAN equivalents `take_salvo_turn/handle_opponent_salvo` (`battleships.py:4886-5043`).
  - Placement rules: straight H/V lines, no overlap (`check_placement` at `battleships.py:1327-1334`), no off-board (`line_cells` at `battleships.py:1274-1279`). Ships may touch (no adjacency ban).
  - Enemy tactics: `Random placement` vs `Contrarian` (samples K=32 layouts, keeps lowest density-score layout, `Board.place_contrarian` at `battleships.py:1361-1372`).
- **Board/fleet presets** (`battleships.py:301-319`, `README.md:100-121`):
  - `FLEET_PRESETS`: `classic` (5/4/3/3/2), `small` (3/2/2), `armada` (5/4/3/3/3/2/2). `SETUP_PRESETS`: Standard 10×10 classic, Skirmish 8×8 small, Grand 12×12 armada. Custom `--board 6-14 --fleet classic|small|armada`.
  - Cells are `A1`-style (`cell_name` at `battleships.py:1163-1164`); tolerant parser accepts `B7`, `7B`, `B 7`, `-`/`,` separators (`parse_cell` at `battleships.py:1167-1205`).
- **Controls** (`README.md:132-186`, `HOW_TO_PLAY/SHOT_HELP/PLACE_HELP` at `battleships.py:2459-2525`):
  - Placement (cursor): Arrows move, `R` rotate, `Enter` place, `Z`/Backspace undo, `Q`/Esc abandon; green `+` valid / red invalid ghost (`interactive_place_fleet` at `battleships.py:2379-2516`).
  - Shooting (cursor): Arrows/WASD, Enter/Space fire, `A–N` column jump, `1–9,0` row jump (0=row 10), `?` hint, `/` density, `W` save, `Q`/Esc surrender (`Game._get_shot_cursor` et seq. at `battleships.py:2849-2932`).
  - Typed fallback (non-TTY): `B7`, `hint`, `map`, `board`, `save NAME`, `help`, `quit`; placement `A1 H/V`, `undo`, `random`. LAN adds `say`, `chat`, `surrender`.

## 3. AI opponents

Registry `LEVELS` (`battleships.py:2185-2191`); base `AI` holds `Knowledge` + `rng`, `choose()`/`record()`/`adjacent_to_hits()` (`battleships.py:1948-1964`).

| Level | Implementation |
|---|---|
| Easy | `EasyAI.choose` (`battleships.py:1967-1969`): uniform `rng.choice(untried)`. |
| Medium | `MediumAI.choose` (`battleships.py:1972-1975`): orthogonal neighbours of `hit` set if any, else random. |
| Hard | `HardAI` (`battleships.py:1978-2008`): if hits, `line_extensions()` walks hit runs to both ends (`battleships.py:1979-1997`), else checkerboard `(r+c) % min(remaining)==0` parity search. |
| Expert | `ExpertAI.choose` → `best_cell()` (`battleships.py:2011-2013`, `1642-1674`). Exhaustive probability map: `_density()` weights each consistent placement by `10**covered_hits` (`battleships.py:1535-1556`); `_coverage()` aggregates per-cell score/count/orientation/`per_len` (`battleships.py:1566-1603`); ties broken by coverage variance then `rng.choice`. `DensityEngine` is a thin static facade (`battleships.py:274-283`). |
| Nightmare | `NightmareAI` (`battleships.py:2147-2180`, defaults `samples=60, candidates=12, z=2.0`): top-12 `top_candidates()`, 60 `random_completion()` consistent fleets (`battleships.py:2042-2082`, 60×100 placement retries), greedy rollout `_greedy_shots()` per candidate (`battleships.py:2085-2132`, Expert-playout to cap 100), `_mc_pick()` significance test vs Expert fallback (`battleships.py:2135-2144`). Slow but strongest; excluded from `--bench` by default. |

Supporting coach systems: `top_candidates(n=3)` (`battleships.py:1613-1629`), `hint_text()` (`battleships.py:1712-1720`), `render_density()` 0–9 normalized (`battleships.py:1632-1709`), `is_coach_opt/coach_line` (`battleships.py:1916-1925`), `show_shot_review/print_coach_summary` (`battleships.py:2549-2610`), `expert_par()` headless Expert replay for “Expert par” (`battleships.py:1723-1739`).

## 4. LAN multiplayer + anti-cheat

- **Transport/lobby:** `LANClient` (`battleships.py:5340-6961`, facade over `PeerRegistry/ChatLog/TcpRateLimiter`). Constants `DEFAULT_LAN_PORT=48785`, `HEARTBEAT_INTERVAL=2.0`, `HEARTBEAT_TIMEOUT=10.0`, `MAX_NET_LINE=65536` (`battleships.py:3474-3479`). UDP broadcast beacons every 2 s, TCP match play, 200-message chat (`ChatLog`, `CHAT_HISTORY_LIMIT`). Password lobby via `lobby_key`/HMAC (`sign_obj/send_json_obj/parse_json_line` at `battleships.py:3525-3560`). Rate-limit 10 conns/5 s per IP. Port scan `base..base+19` in `_bind_sockets()` (`battleships.py:5478-5520`).
- **Deterministic match setup:** `derive_match_params()` (`battleships.py:3700-3725`) hashes sorted `(id,nonce)` pairs → mode (shared pref wins, else `digest[0]&1`), first player (`digest[1]%2`), `match_id`, HMAC `key`. No host advantage (`README.md:281-288`, `LANGame.play_loop` at `battleships.py:5068-5110`).
- **Protocol:** `PROTOCOL_TYPES = {ready, shot, salvo, shot_result, salvo_result, reveal, end, abort, cheat}` (`battleships.py:3989-3999`); framed newline JSON with per-message HMAC. `MatchConnection` (`battleships.py:4002-4185`) owns reader/heartbeat threads, `queue.Queue` inbox, `ping/pong`, chat/surrender fast-path, >100 bad-MAC line disconnect.
- **Anti-cheat (two tiers):**
  - *Classic (default):* `ready` exchanges `SHA256(canonical reveal JSON)` (`make_board_reveal/board_commit_hash` at `battleships.py:3605-3617`); end-game full reveal is shape-checked (`normalize_board_reveal` at `battleships.py:3563-3602`), geometry-checked (`reveal_to_board` at `battleships.py:3620-3654`), and shot-log replayed (`verify_shot_log` at `battleships.py:3657-3697`). Mismatch → honest side gets forfeit win + `Opponent protocol violation: <reason>` (`LANGame._opponent_cheat` at `battleships.py:4283-4289`).
  - *Per-cell (`anticheat cell` both sides):* `make_cell_commitments()` commits `SHA256([r,c,bit,salt])` per cell + root hash (`battleships.py:3766-3779`); per-shot proofs verified by `verify_cell_reveal()` (`battleships.py:3790-3808`); sunk inference via `verified_sunk_cells`; final full-bitmap check `verify_cell_final()` + fleet-shape solvers `bits_form_fleet/board_from_bits/all_lines_through/cells_form_line` (`battleships.py:3811-3941`). Exchange in `LANGame.exchange_ready()` (`battleships.py:4576-4633`).
- **End states:** `verified win` (crypto verified) vs `forfeit win` (surrender/disconnect/no-reveal/cheat); `lan_score` tracks `win/loss/verified_win/forfeit_win` (`battleships.py:5385-5388`, `7071`).

## 5. Visual / terminal UI system

- **Capability detection:** `supports_cursor_ui()` (stdin+stdout TTY, `battleships.py:378-382`), `set_terminal_state()` (`battleships.py:126-132`), `--no-color` / `NO_COLOR=1` (`battleships.py:7053-7054`). Non-TTY degrades to numbered typed menus (`select_menu` fallback at `battleships.py:1109-1120`) and typed shooting/placement.
- **Primitives:** `paint()` ANSI wrapper (`battleships.py:361-364`), `cursor_reverse()` (`battleships.py:367-370`), `clear()` (`battleships.py:373-375`), box kit (`_vis_len/_pad_vis/box_top_line/boxed_panel/side_by_side/title_banner/big_banner/fleet_panel` at `battleships.py:646-753`).
- **Game feel (21 flags):** `VISUAL_DEFAULTS` (`battleships.py:396-418`) — `animations, explosions, shot_trails, sunk_reveal, animated_water, last_shot_highlight, fleet_status, turn_banners, radar_spinner, victory_cinematics, color_density, screen_flash(OFF), terminal_bell(OFF), screen_shake, hit_stop, kill_cam, damage_fire, sonar_sweep, captain_taunts, streaks, epic_mode`; edited in `visual_settings_menu()` (`battleships.py:587-633`). Gated by `vis()`/`can_animate()` (`battleships.py:428-433`).
- **Effects:** `_burst_frames/_hit_stop/_screen_shake/_shot_trail/burst_banner` (`battleships.py:462-526`), `burst_shot()` solo (`battleships.py:833-906`, streak/taunt logic) vs `burst_shot_lan()` (`battleships.py:909-955`), `show_sunk_reveal()` kill-cam (`battleships.py:544-573`), `finish_cinematic()` (`battleships.py:576-584`), `Spinner` radar/sonar (`battleships.py:756-814`), animated water `water_char()` (`battleships.py:528-533`), burning ships in `own_char/track_char` (`battleships.py:2201-2250`), `TAUNTS` per level (`battleships.py:1746-1767`).
- **Boards:** `render_boards()` side-by-side YOUR FLEET / ENEMY WATERS (`battleships.py:2284-2291`), `render_own()` with ghost preview (`battleships.py:2294-2321`), LAN variant `render_lan_boards/remote_char` (`battleships.py:3948-3986`). Legend symbols `~/S/X/o/#/·/@/+` (`legend()` at `battleships.py:2328-2331`).
- **Input:** `KeyReader` raw cbreak reader with Win (`msvcrt`) + POSIX (`termios/tty/select`, UTF-8 reassembly) paths (`battleships.py:986-1106`); `select_menu()` cursor menu (`battleships.py:1109-1156`).

## 6. Save/load, CLI flags, benchmark mode

- **Save/load (JSON, `SAVE_VERSION=1`):** `board_to_dict/board_from_dict` (`battleships.py:1839-1857`), RNG-state helpers `_encode_state/_decode_state` (`battleships.py:1860-1867`), `save_game()` persists `level/mode/turn/stats/player/enemy/random_state/ai_state` (`battleships.py:1870-1885`), `load_game()` rebuilds `Game` via lazy `LEVELS` lookup + `Knowledge.from_board()` + RNG restore (`battleships.py:1888-1915`). In-game `W` / `save FILE`, resume `--load FILE` (`README.md:208-216`). LAN and Hotseat/Campaign are not saveable (solo `Game` only).
- **CLI (`main()` argparse at `battleships.py:7009-7029`):** `--no-color`, `--board N` (6–14), `--fleet classic|small|armada`, `--contrarian`, `--campaign`, `--load FILE`, `--lan-port` (default 48785), `--lan-password`, `--bench N`, `--seed S` (default 0), `--include-nightmare`. Skips setup menu when board/fleet given (`battleships.py:7059-7064`).
- **Benchmark (headless):** `--bench N` runs `bench_solo()` per level (`battleships.py:1796-1809`, seeded `bench-<seed>-layout/ai-<g>` RNG streams) printing median + raw shots, then `bench_match()` round-robin (`battleships.py:1812-1833`) and win totals (`battleships.py:7031-7051`). Nightmare excluded unless `--include-nightmare` (Monte Carlo cost).

## 7. Notable bugs, tech debt, single-file constraints

- **Single-file trade-offs (acknowledged):** `README.md:332` mandates single-file/zero-dep for contributions. Mitigations in place: `Protocol` contracts, `PlacementCache/PeerRegistry/ChatLog/TcpRateLimiter/DensityEngine` extractions, `Game` turn-helper splits (`_take_player_salvo/_enemy_*_response`). Residual God-objects: `Game` (~500 lines), `LANGame` (~1100 lines: `battleships.py:4187-5320`), `LANClient` (~1600 lines: `battleships.py:5340-6961`). Dual global+dataclass config (`SIZE` vs `_CONFIG`, `VISUAL` vs `_VISUAL_SETTINGS`, `PLACEMENTS` vs `_PLACEMENT_CACHE`) risks drift; only `configure_board()`/`_rebuild_placements()` keep them in sync.
- **Correctness/robustness notes (from code reading, not fuzzing):**
  - `Board.fire()` uses `assert` for double-fire (`battleships.py:1375`); `python -O` would silently allow corruption. Callers guard via `enemy.shots` checks, but salvo `apply_salvo` relies on pre-dedup.
  - `Knowledge.record()` does `remaining.remove(sunk_len)` (`battleships.py:1471`) — a duplicate/lie sunk report raises `ValueError`; no defensive guard.
  - `Knowledge._assignments/_leftover_ok` is exponential constraint search (`battleships.py:1496-1532`); fine for ≤7 ships but worst-case blowup on large custom boards; Nightmare `random_completion` caps at 60×100 retries and may return `None` (falls back to Expert — correct but weaker).
  - Save format is version-strict (`doc["version"] != 1` rejects, `battleships.py:1896`); no migration path. `ai_state` only restored when `ai.rng is not random` (`battleships.py:1909-1910`); default-AI resume reseeds nondeterministically despite saving global RNG.
  - Hotseat/LAN ignore custom boards (hard reset to 10×10 classic in `main()`, `battleships.py:7173-7180`); LAN `normalize_board_reveal` also validates against global `SHIP_LEN/FLEET` (`battleships.py:3573-3578`), so mixed-fleet peers would fail verification by design.
  - Broad `except Exception: pass` swallowing is pervasive in visual/network paths (60+ sites, e.g. `battleships.py:436-503, 5408-6307`); good for game-feel resilience, bad for debuggability.
  - Minor: `TAUNTS["losing"]` pools are dead data in V1 (`battleships.py:1742-1745` admits `burst_shot` lacks board context); `Spinner._run` swaps to braille chars when `radar_spinner` is *off* (`battleships.py:800-803`, inverted naming); `GameStats.streak` is unused while `_BURST_STREAK` dict carries display streak (`battleships.py:1769-1777`).
- **Security posture:** HMAC per-message + commit-reveal is sound for casual LAN play, but classic mode trusts the shot stream until end-game replay; per-cell mode closes this at 10 000-commitment (≈100×100 `cell_commitments` dict) bandwidth cost per `ready`. `MAX_NET_LINE=65536` bounds one line but per-cell `ready` with full dict approaches that budget on large boards.

## Project layout

```text
.
├── battleships.py   # the game itself (7215 lines)
├── README.md        # user docs (features, modes, LAN, CLI)
├── ANALYSIS.md      # this file
└── save.json        # user-created on save (gitignored, not committed)
```
