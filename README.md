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
- Optional visual themes (full-screen restyle: title art, subtitles, panel borders, board colors, icon HUD) via `pip install rich` — classic look is the default even with rich installed; opt in via `Settings → Theme` or `--theme`
- Save / resume games as JSON
- Headless AI benchmark mode

## Requirements

- Python 3.8+
- No pip packages required (optional `rich` package unlocks visual themes: `pip install rich`)
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

1. `Singleplayer` → `Play vs AI` | `Campaign (Easy → Nightmare)`
2. `Multiplayer` → `Hotseat (2 players)` | `LAN Matchmaking` | `Online Match`
3. `How to play`
4. `Settings`
5. `Quit`

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

## Settings

`Main Menu → Settings` has three groups:

- `Input`: `typed_mode (OFF)` — typed input only: no arrow-key menus or
  cursor aiming, everything becomes numbered/typed commands. Ideal for
  mobile keyboards (Pydroid 3) and touch terminals.
- `Visuals`: `animations, explosions, shot_trails, sunk_reveal, animated_water, last_shot_highlight, fleet_status, turn_banners, radar_spinner, victory_cinematics, color_density, screen_flash (OFF), terminal_bell (OFF)`
- `Theme`: `classic | abyss | arcade | harbor` (session-only, default
  `classic`). Each theme restyles the whole screen: title art + subtitle,
  single/double panel borders in theme colors, board headers, water color
  and motion, ship/unknown colors, icon set, and fleet-panel labels
  (e.g. abyss uses `BOAT MANIFEST` / `SONAR CONTACTS`). `classic` stays
  active even when `rich` is installed — the menu shows
  `Classic look — richer themes in Settings → Theme`; without `rich` it
  shows `Install rich for better looks: pip install rich`. Themed frames
  need `pip install rich`; water/icons still vary without it. Force the
  classic look with `--no-rich`.

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
| `--theme NAME` | Visual theme: `classic` \| `abyss` \| `arcade` \| `harbor` (default `classic`; unknown falls back to `classic`) |
| `--no-rich` | Disable Rich theming, use ANSI fallback |
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

Interactive lobby is a live screen: PLAYERS (left) and CHAT (right) stay
side-by-side and auto-refresh about once per second — no manual refresh.

```text
Up/Down    select player      Enter      player actions
1-9        quick challenge    C          public chat
T          private to select  R          review requests
A          accept first       G          start ready match
X          cancel outgoing    S/U        settings / status
H/?        help               L          full chat log
/ or :     one command        E          advanced command line
Q          quit
```

Non-TTY falls back to a typed loop that prints a players+chat snapshot
after every command.

### Lobby commands (also via `/` and `E` advanced mode)

### In-game LAN

Turn order/mode is derived deterministically from both players — no host advantage.

Interactive extras vs solo: `T` talk, `L` chat history, `Q/Esc` surrender.

End states: `verified win` (board cryptographically verified) vs `forfeit win` (surrender / disconnect / no reveal / cheat).

### Anti-cheat

- Classic (default): SHA256 board commitment at `ready`, full reveal + shot-log replay at end.
- Per-cell (`anticheat cell` on both sides): per-cell `SHA256([r,c,bit,salt])` commitments, per-shot proofs, final full-board verification.

Mismatches award the honest side a forfeit win with `Opponent protocol violation: <reason>`.

## Online play

Peer-to-peer internet play with no server: `Main menu → Multiplayer → Online Match → Host / Join`.
New here? `Online Match → Play online (guided setup)` asks two plain questions,
probes your network once, and jumps into the right flow; `VPN setup help` walks
through Tailscale (any VPN works — ZeroTier, WireGuard, Hamachi and others are
fine). Power users keep the raw host/join/settings flows under
`Advanced Networking`.
Single file, zero dependencies, standard library only. Direct flows use TCP; hole
punch uses UDP (RUDP-lite) with one socket carrying STUN, punching and game traffic.
In-game, `Online Match → How online play works` shows this same guide as boxed
windows (two sections per page). Everything the `--online` CLI flags do is also in
`Online → Settings`, so nothing is CLI-only.

### Before you start (both players)

1. Run the **same game version** — a mismatch refuses to connect.
2. Pick the **same board, fleet, mode and anti-cheat setting**.
3. Decide who **hosts** (shares first) and who **joins**.
4. Have a chat/call ready — you read codes to each other.
5. Keep the terminal at least 40 columns wide; codes are long.

### Flow 1 — Direct code (easiest when it applies)

Use when: same VPN, or the host has a port forward / UPnP router.

- **Host:** `Online Match → Host → Host direct code (TCP)`. The game tries UPnP
  automatically unless `Settings → UPnP` is OFF (lease 3600 s, removed on exit).
  Read the invite code — plus the manual `host:port` line — to the guest.
- **Guest:** `Online Match → Join → Paste invite code`. Play.
- **VPN tip:** join the VPN first (Tailscale, ZeroTier, WireGuard) and use the VPN
  address. It always works. Confirm out loud — "are you on the VPN address?" —
  before debugging anything else.

### Flow 2 — Hole punch (both behind home routers)

Use when: no VPN, no port forward. Works on most home routers
(endpoint-independent mapping). Fails on symmetric NAT and many mobile hotspots.

- **Host:** `Host → Host hole-punch offer`. Read the STUN line: `punchable` means
  the offer will likely work; anything else, read the reason. Share the **offer**.
- **Guest:** `Join → Paste the offer`. The game STUNs your NAT and shows **your
  answer code** — read it back to the host.
- **Host:** paste the answer when asked. Both sides punch through (authenticated
  `PUNCH` / `PUNCH-ACK`, ~15 s window).
- **Guest:** press Enter only **when the host has your answer**, then both punch.
  Names and rules verify (UDP READY, mirroring TCP READY) and the game starts.

### Flow 3 — Manual (IPv6, DNS names, odd setups)

- **Host:** host direct as in Flow 1, but read the **manual line**: `host:port`
  **plus** the secret line (two parts, both needed).
- **Guest:** `Join → Manual address`. Type `host:port`, then the secret.
  Accepts `203.0.113.9:51234` and `[2001:db8::1]:51234`.

### Reachability check (the host sees this first)

The host menu probes UPnP + STUN in parallel and prints one line:

- `UPnP: found (external IP)` — direct codes should work.
- `UPnP: CGNAT` — the mapping is useless even when the router agrees; punch or VPN.
- `STUN: punchable at ip:port` — offers should work.
- `STUN: anything else` — read the reason; try VPN/manual.

Then pick the offered flow. Manual is always offered.

### Settings & flags (menu = CLI, nothing CLI-only)

`Online → Settings` mirrors every flag: name (20 chars), port (`0` = random),
bind address, resume window (s), mode single/salvo, anti-cheat hash/cell, net
debug (redacted logs), STUN servers (repeatable, default public Google servers),
UPnP ON/OFF.

```bash
python3 battleships.py --online host
python3 battleships.py --online join CODE
python3 battleships.py --online host --port 51234 --name Ada --stun stun.example.com:3478
```

| Flag | Description |
|---|---|
| `--online host\|join CODE` | Host or join an online match |
| `--port N` | Online TCP/UDP port (0 = random high port) |
| `--bind ADDR` | Bind address (default all interfaces) |
| `--stun HOST:PORT` | STUN server, repeatable (default public Google servers) |
| `--no-upnp` | Disable UPnP port mapping |
| `--resume-timeout SEC` | Reconnect window in seconds (default 120) |
| `--net-debug` | Verbose redacted network logs |
| `--name NAME` | Display name (max 20 chars) |

### Codes, security, resume

- Codes **burn** after first use (or three bad tries) — ask for a fresh one, never
  retry a dead code. `0`/`1`/`8` typos are forgiven via CRC check.
- Offers carry no names; the READY exchange swaps real names and checks rules.
- Auth is HMAC (SHA256). There is **no encryption** and **no server** — keep codes
  private, since anyone holding one can take the seat.
- Dropped link? TCP auto-reconnects inside the resume window (default 120 s); UDP
  needs a manual redial. A clean exit reports `Opponent left`; a dead link reports
  `Connection lost` and reconnects automatically inside the window.

### Error messages

- `Connection refused` — the host is not listening, or a firewall is rejecting.
  Check address, port and VPN membership.
- `Timeout` — packets are being dropped by a NAT or firewall; try a VPN or hole punch.
- `Wrong code` — check the invite and try again (get a fresh one if burned).
- `Version mismatch` — both players must run the same game version.
- `Rules mismatch` — board size / fleet / mode differ (align anti-cheat too).
- `Opponent left` / `Connection lost` — clean exit vs dropped link (reconnects
  automatically inside the resume window).
- Punch timing out with no two-way path usually means symmetric NAT — use a VPN,
  a port forward, or UPnP direct mode.
- Pasting an answer code into join? Give it to the **host**, not the join box.

### Which NAT are you behind?

- **Home router (full cone)** — direct + punch both work.
- **Symmetric / phone hotspot** — punching fails by design. Don't retry: use VPN.
- **CGNAT (carrier-grade NAT)** — UPnP mapping is useless even on success; the menu
  says so. Punch or VPN instead.

## Troubleshooting

- Bad colors → `--no-color` or `NO_COLOR=1`.
- No cursor menus → not a TTY, or `Settings → Input → typed mode` is ON. Use typed commands (`B7`, `hint`, `map`, `save`, …).
- Narrow terminal (phone) → long help/chat/review texts wrap to fit and HUD rows stack; boxes and addresses stay intact. Minimum usable width is 40 columns.
- `ship length X doesn't fit` → board too small for fleet, increase `--board` or use smaller `--fleet`.
- No LAN peers → same subnet, UDP broadcast allowed, same port range + password, firewall open for UDP+TCP.
- Online stuck → full checklist is `Online Match → How online play works` (§ Troubleshooting checklist). Short version: same version/rules? fresh code? VPN address? hotspot/symmetric NAT (use VPN)? CGNAT (ignore UPnP, punch/VPN)?
- `No valid reply` → peer busy, wrong IP:port, or password mismatch.
- Nightmare slow → expected (Monte Carlo). Use Expert for fast strong play.

## Project structure

```text
.
├── battleships.py   # the game itself (single file, zero dependencies)
├── README.md               # this file
└── LICENSE                 # MIT
```

Save files (`save.json`) are created by you when saving and are not committed.

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

