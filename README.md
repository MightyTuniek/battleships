# Battleships — Terminal Edition + LAN

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![No dependencies](https://img.shields.io/badge/dependencies-none-green)
![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macOS%20%7C%20windows-lightgrey)

A complete Battleships game for the terminal in a single Python file. Play against 5 AI difficulties, run a campaign, play 2-player hotseat, or play over LAN with chat and anti-cheat. No dependencies, no install.

## Features

- Solo vs AI (5 levels: Easy to Nightmare Monte Carlo)
- Campaign mode (Easy → Nightmare)
- Hotseat 2-player on one terminal
- LAN multiplayer with discovery, chat, and cryptographic anti-cheat
- 3 board/fleet presets + custom 6×6 to 14×14 boards
- Normal and Salvo (one shot per afloat ship) rules
- Probability hints, density map, coach scoring, Expert par, shot review
- Animated terminal UI with colors, with full typed fallback for non-TTY
- Save / resume games as JSON
- Headless AI benchmark mode

## Requirements

- Python 3.8+
- No pip packages required
- Works best in a real terminal (TTY) for cursor menus, colors, and animations
- Linux / macOS / Windows supported

Check your version:

```bash
python3 --version
```

## Installation

Clone and run — no install step:

```bash
git clone https://github.com/MightyTuniek/battleships
cd battleships
python3 battleships.py
```

If colors look wrong:

```bash
python3 battleships.py --no-color
```

## Quickstart

```bash
python3 battleships.py
```

Main menu:

1. `New game (vs computer)`
2. `Campaign (Easy → Nightmare)`
3. `Hotseat (2 players)`
4. `LAN Matchmaking`
5. `How to play`
6. `Visual Settings`
7. `Quit`

You fire first in solo games. First to sink the entire enemy fleet wins.

## Game modes

### Solo vs AI

Flow: setup → difficulty → mode → tactic.

- `Normal`: 1 shot per turn.
- `Salvo`: shots per turn = number of your ships still afloat. Same for the enemy.
- `Random placement`: enemy ships anywhere.
- `Contrarian`: enemy samples 32 layouts and keeps the lowest-probability one — hides where AI looks last.

After win/loss you can choose `Review your shots?` for a coach breakdown.

### Campaign

Plays `Easy → Medium → Hard → Expert → Nightmare` in sequence, reusing your fleet each mission. One loss ends the run. Shows per-mission results and totals.

Start directly:

```bash
python3 battleships.py --campaign
```

### Hotseat

2 players, same terminal, with handoff screen. Currently fixed to 10×10 classic fleet.

### LAN multiplayer

See [LAN multiplayer](#lan-multiplayer) below for full lobby, chat, and anti-cheat docs.

## Board setups and fleets

In-menu `GAME SETUP`, or via `--board` / `--fleet`:

| Preset | Board | Fleet |
|---|---|---|
| `Standard` | 10×10 | Carrier(5), Battleship(4), Cruiser(3), Submarine(3), Destroyer(2) |
| `Skirmish` | 8×8 | Cruiser(3), Submarine(2), Destroyer(2) |
| `Grand` | 12×12 | Carrier(5), Battleship(4), Cruiser(3), Submarine(3), Destroyer(3), Frigate(2), Patrol(2) |

Custom:

```bash
python3 battleships.py --board 8 --fleet small
python3 battleships.py --board 12 --fleet armada
```

- Board size is clamped to 6–14.
- Fleets: `classic` | `small` | `armada`.
- Cells are `A1` style: column letter + row number, e.g. `B7`.
- Ships are straight horizontal/vertical lines, no overlap, no off-board.

## AI opponents

| Level | Behavior |
|---|---|
| `Easy` | Random untried squares. |
| `Medium` | Random search, hunts orthogonal neighbors of hits. |
| `Hard` | Checkerboard/parity search, follows hit lines, extends line ends. |
| `Expert` | Probability map over all legal placements, shoots highest score. |
| `Nightmare` | Monte Carlo rollouts over consistent fleets, picks fewest expected shots. Slow but strongest. |

## Controls

### Symbols

```text
~ water   S your ship   X hit (yellow=enemy hit, red=your ship hit)
o miss    # sunk enemy ship   . unfired enemy water
@ cursor / reveal   + placement preview
```

Left: `YOUR FLEET`. Right: `ENEMY WATERS`.

### Ship placement (interactive)

```text
Arrows           move cursor
R                rotate horizontal / vertical
Enter            place ship
Z / Backspace    undo
Q / Esc          abandon
```

Green `+` = valid, red = invalid. `Enter` = start, `R` = reroll on random-layout screen.

### Shooting (interactive)

```text
Arrows / WASD                       move cursor
Enter / Space                       fire
A–N (max, depends on board size)    jump to column
1–9, 0                              jump to row (0 = row 10)
?                                   toggle hint
/                                   toggle density map
W                                   save game
Q / Esc                             abandon / surrender
```

You can also type `B7` + `Enter` anytime.

### Typed fallback (pipes / non-TTY)

```text
B7               fire at B7
hint             top-3 suggestions
map              density map
board            redraw
save NAME        save to file, e.g. save game.json
help             help
quit             abandon
```

Placement typed: `A1 H` / `A1 V`, `undo`, `random`, `help`.

LAN typed adds: `say <msg>`, `chat`, `surrender`.

## Coach, hints, density, par

- `?` / `hint`: top-3 expert cells with scores.
- `/` / `map`: normalized 0–9 density map. `X` = hit, `o` = miss.
- Coach: every shot is graded against expert top-3. End screen shows optimal %.
- Shot review: turn-by-turn `HIT/miss/SUNK` plus what the top-3 were for off-optimal shots.
- Expert par: on win, a headless Expert plays your exact enemy layout to benchmark you: `You won in N shots. Expert par: M.`

## Visual settings

`Main Menu → Visual Settings`:

`animations, explosions, shot_trails, sunk_reveal, animated_water, last_shot_highlight, fleet_status, turn_banners, radar_spinner, victory_cinematics, color_density, screen_flash (OFF), terminal_bell (OFF)`

Disable color:

```bash
python3 battleships.py --no-color
NO_COLOR=1 python3 battleships.py
```

## Save / load

Games save as portable JSON (boards, turn, stats, RNG state for exact resume).

In game: `W` or `save <file>`. Resume:

```bash
python3 battleships.py --load save.json
```

## CLI reference

```bash
python3 battleships.py --help
```

| Flag | Description |
|---|---|
| `--no-color` | Disable ANSI colors |
| `--board N` | Board size 6–14, skips setup menu |
| `--fleet NAME` | `classic` \| `small` \| `armada`, skips setup menu |
| `--contrarian` | Force contrarian enemy placement |
| `--campaign` | Start campaign directly |
| `--load FILE` | Resume saved game |
| `--lan-port PORT` | Base LAN port, default `48785` (tries `PORT..PORT+19`) |
| `--lan-password SECRET` | Pre-set LAN lobby password |
| `--bench N` | Headless benchmark: N games per level, median + round-robin |
| `--seed S` | Seed label for `--bench` (default `0`) |
| `--include-nightmare` | Include Nightmare in `--bench` (slow, excluded by default) |

Examples:

```bash
python3 battleships.py --board 8 --fleet small
python3 battleships.py --campaign --contrarian
python3 battleships.py --load mygame.json
python3 battleships.py --bench 20 --seed 42
python3 battleships.py --bench 5 --include-nightmare
python3 battleships.py --lan-port 48785 --lan-password secret123
```

The primary entry point is `battleships.py`.

## LAN multiplayer

LAN resets to 10×10 classic. Discovery via UDP broadcast beacons every 2s, TCP match play, heartbeat every 2s, 200-message chat history.

### Lobby commands

```text
list                 show discovered players
requests             show incoming requests
request <#|IP[:port]> send match request
accept <#>           accept
reject <#>           reject
cancel               cancel outgoing / pending
pref normal|salvo    mode preference
name <name>          display name (max 24)
password <secret>    enable password lobby
password             clear password
anticheat cell       prefer per-cell commitments
anticheat off        prefer classic hash commitments
status               security + score
say <message>        public chat
tell <#> <message>   private chat
chat                 history
start                start pending match
help                 help
quit                 leave LAN
```

Interactive lobby also has menus for Players, Requests, Chat, Settings, Status, Help, and Advanced command line.

### In-game LAN

Turn order/mode is derived deterministically from both players — no host advantage.

Interactive extras vs solo: `T` talk, `L` chat history, `Q/Esc` surrender.

End states: `verified win` (board cryptographically verified) vs `forfeit win` (surrender / disconnect / no reveal / cheat).

### Anti-cheat

- Classic (default): SHA256 board commitment at `ready`, full reveal + shot-log replay at end.
- Per-cell (`anticheat cell` on both sides): per-cell `SHA256([r,c,bit,salt])` commitments, per-shot proofs, final full-board verification.

Mismatches award the honest side a forfeit win with `Opponent protocol violation: <reason>`.

## Troubleshooting

- Bad colors → `--no-color` or `NO_COLOR=1`.
- No cursor menus → not a TTY, use typed commands (`B7`, `hint`, `map`, `save`, …).
- `ship length X doesn't fit` → board too small for fleet, increase `--board` or use smaller `--fleet`.
- No LAN peers → same subnet, UDP broadcast allowed, same port range + password, firewall open for UDP+TCP.
- `No valid reply` → peer busy, wrong IP:port, or password mismatch.
- Nightmare slow → expected (Monte Carlo). Use Expert for fast strong play.

## Project structure

```text
.
├── battleships.py   # the game itself
├── README.md               # this file
└── save.json               # created by you when saving (not committed)
```

Save files are user-created and should not be committed. Suggested `.gitignore`:

```gitignore
__pycache__/
*.pyc
*.json
```

## Contributing

Issues and PRs welcome:

1. Fork the repo
2. Create a branch: `git checkout -b feature/my-change`
3. Run a syntax check: `python3 -m py_compile battleships.py`
4. Optional AI check: `python3 battleships.py --bench 5 --seed 0`
5. Open a pull request

Please keep the single-file, zero-dependency constraint.

## License

MIT - see [LICENSE](LICENSE)

