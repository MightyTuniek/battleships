# NOTES-online.md — Online P2P (T0–T6 scope)

## T0 Discovery (2026-10-10)

- Entry point + CLI: `battleships.py:9664` `main()`, argparse `9667-9691`.
  Flags: `--no-color --bench --seed --include-nightmare --load --lan-port
  (default 48785) --lan-password --board --fleet --contrarian --campaign
  --theme --no-rich`. Menu LAN entry `9910-9918`.
- LAN connect/framing (`battleships.py`):
  - `send_json_obj():5416` — `sign_obj()` HMAC-hex (`5407`) + canonical
    `json.dumps(sort_keys, separators)` + `\n` + `sendall` (opt lock).
  - `parse_json_line():5427` — `json.loads`, optional HMAC verify with
    `hmac.compare_digest`, returns None on bad MAC/JSON.
  - `MatchConn:5890` — sync blocking socket, `send_lock`, `queue.Queue`,
    `_read_loop():6000` (`makefile("r")`, `readline(MAX_NET_LINE=65536)`),
    `_heartbeat_loop():6066` ping every `HEARTBEAT_INTERVAL=2.0`,
    dead after `HEARTBEAT_TIMEOUT=10.0` (`5245-5246`).
  - `LANClient:7347` — UDP beacon/broadcast + TCP accept on
    `base_port..+19` (`7456-7512`), `LAN_VERSION=1` (`5241`),
    `create_connection` timeouts 3s/5s (`8064,8085`).
- Anticheat produced/consumed:
  - Classic: `board_commit_hash():5491` over `reveal_payload_json():5487`;
    `make_board_reveal():5495`, `normalize_board_reveal():5445`,
    `reveal_to_board():5502`, `verify_shot_log():5539`; exchanged in
    `LANGame` setup/exchange/finalize (`6077+`, `6234-6554`, `7125-7253`).
  - Per-cell: `cell_commit_hash():5642`, `make_cell_commitments():5647`,
    `verify_cell_final():5786`; flag `conn.cell_anticheat` (`5906`),
    negotiated via `cell_anticheat` in lobby msgs (`8097,8217,8231,8314`).
  - Match key/turn: `derive_match_params():5582` — deterministic mode +
    first-move from both nonces (no host advantage; no extra coin flip needed).
- Loop sync or async: **synchronous + threads**. `threading.Thread(daemon)`
  for read/heartbeat/accept/beacon (`5926-5927,7456-7458`), `queue.Queue`
  for inbound (`5910`). No `asyncio` in repo.
- Test runner/command: **none**. No `tests/`, no pytest/unittest config.
  Baseline: `python -m py_compile battleships.py` → OK (2026-10-10,
  Python 3.14.7). `README` contributing suggests same + optional bench.
  New suite: `python -m unittest discover -s tests -v` (stdlib only).
- Minimum Python: README/code say **3.8+** (`from __future__ import
  annotations`, dataclasses). Spec wants 3.9+. `net/` targets 3.9-compatible
  syntax (no `X | Y` at runtime, no `match`); verified on 3.14. No stop needed.
- Opponent names/chat rendering:
  - `LANClient.print_now():7419`, `add_chat():7428` (via `ChatLog:233`),
    `handle_match_chat` → match chat, lobby `say/tell/chat/history`
    (`8456+,8549,8659-8763`), in-game `T/L` extras. Rendering via plain
    `print` + `wrap_prose():1297` / `page_lines():1393`; **no sanitization**
    of peer strings today (T6 adds `net/sanitize.py`).

## Decisions

- SINGLE-FILE (user hard constraint, overrides spec `net/` layout): the
  whole online stack lives in `battleships.py` under the
  `Online P2P mode (stdlib only, single-file)` section (~line 9663+,
  before `main()`). No `net/` package. Ruling cost if wrong: none for
  behavior; spec cross-references to `net/*.py` map to section headers
  in the single file. Tests import `battleships` directly.
- Concurrency follows repo (spec §0 default): sync blocking sockets +
  threads + `queue.Queue` facade. No `asyncio` loop in T0–T6. Recorded as
  deviation from §3/§6 `asyncio.start_server` wording; behavior (1 peer,
  timeouts, single writer) preserved.
- Import rule: single file is already stdlib-only (only optional `rich`
  in UI code, which online code never touches). New imports are stdlib
  only (`struct/base64/zlib/ipaddress` added to the existing set).
- T1 LAN refactor: **zero edits** to existing LAN code (diff is purely
  additive). `LanTransport` reuses the exact `sign_obj` byte recipe;
  golden test pins bytes.
- `derive_match_params` already gives fair first-move/mode → T6 needs no
  new coin flip; `rules_hash` is canonical JSON of
  `{size, fleet, mode}` compared at READY.
- T6 e2e is headless (real TCP loopback sockets + real anticheat payloads
  through threads, not subprocess UI drivers): same wire, no flaky UI.

## Implementation rulings (found during T1–T6 verification)

- Windows close deadlock: closing a `makefile` reader from another thread
  while it blocks in `readline` hangs forever. `LanTransport.close()`
  only shuts down + closes the socket; the reader thread (2s socket
  timeout) closes its own file on exit. Same pattern for `TcpTransport`
  (shutdown before close). Cost if wrong: hung games on Windows.
- `Session.recv_game` on `bye` now raises with `reason="PEER_LEFT"` (was
  only in the message string, reason defaulted to `LINK_LOST`).
- Sanitizer also strips OSC sequences (`ESC ] ... BEL`); plain CSI
  stripping left `0;title` residue.
- Test-side corrections (no prod impact): raw-socket handshake tests speak
  guest-first (HELLO/CHALLENGE/bad-AUTH); shot-log `sunk_len` is the ship
  length per `verify_shot_log`; reveal/log pairing is host-log↔guest-board;
  session ack state is arrival-based, so resume tests kill-then-drain.
- Removed dead `read_sealed_frame` helper (handshake inlines it).

## Progress

| ID | Task | Status | Notes |
|----|------|--------|-------|
| T0 | Discovery | done | this file |
| T1 | Transport boundary | done | `Transport`/`LanTransport` in-file; golden test, LAN diff +0/-0 |
| T2 | Constants/errors/crypto/invite | done | exact §3.1 names in-file; known-answer tests |
| T3 | Framing + TCP transport | done | `seal`/`FrameDecoder`/`TcpTransport`; loopback tests |
| T4 | TCP handshake | done | HELLO/CHALLENGE/AUTH/READY, burn, 1-peer |
| T5 | Session layer | done | `Session` seq/ack/heartbeat/resume; fake-clock tests |
| T6 | Game integration/CLI/sanitize | done | `--online`, `OnlineConn`, sanitize, headless e2e |
| T7–T12 | STUN/RUDP/punch/UPnP/UX | deferred | follow-up after T0–T6 verified |

## Blockers / open questions

- None blocking T0–T6. Real-network NAT matrix (§16) is manual, deferred to T12.
