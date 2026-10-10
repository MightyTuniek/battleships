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
| T7 | STUN client + NAT check | done | RFC5769 vectors, fake-server retry/rotation/symmetric tests |
| T8 | Offer/answer codes | done | manual-signaling blobs, tag verification, punch_keys |
| T9 | RUDP + UDP transport | done | UdpTransport, 1000-msg spec-profile proxy run, Karn + fast retransmit |
| T10 | Hole punch | done | cone/symmetric NAT doubles, migration, punched-UDP game e2e |
| T11 | UPnP client | done | fake IGD, mapping args, CGNAT, atexit+signal cleanup |
| T12 | UX flow/messages/README | done | reachability, host planner, code-kind, README section, this report |

## T7–T12 rulings (deviations from the spec work order)

- R4: `STUN_DEFAULT_PORT = 3478` added beside the spec-exact constants
  (§3.1 table is silent on the default-port value; named beats magic).
- R5: RUDP RTO floor = `RUDP_ACK_DELAY_S` (0.02 s granularity floor).
- R6: fast retransmit on 3 duplicate cumulative acks (wire-compatible
  addition; keeps the loss-torture runtime practical; dup resends are
  harmless via dedup).
- R7: punch failure uses reason TIMEOUT (§11 has no punch code; the T12
  message names symmetric NAT explicitly).
- R8: no new punch session-builder wrappers — menu flows compose the
  already-tested primitives (`punch_connect` + `UdpTransport` + `Session`,
  the same lines the T10 e2e proves). Thin untested glue is worse.
- R9: guest starts punching after the user confirms the host has the
  answer (spec wants punching at answer display; a blocking 15 s window
  while the user reads would expire on slow relays).
- Punch display names fall back to local config name + "Guest"/"Host":
  offer/answer carry no names (§8.2); the UDP READY exchange then swaps
  real names and checks rules, mirroring TCP READY.
- Manual `host:port` form accepts `[ipv6]:port` and `host:port`
  (best-effort IPv6; hole punching stays IPv4-only per §8.1).
- Test prefixes are `test_online_*` (repo convention from T1–T6), not
  `test_net_*`; fake STUN/proxy/IGD live inside the test modules.

## Final report (spec §17 T12)

Deviations: single-file layout, sync style, and R4–R9 above. Nothing
deferred: T0–T12 all implemented and tested. Out-of-scope per spec §1
(unchanged): relay/matchmaking server, accounts, telemetry, encryption
(TLS-PSK), IPv6 hole punching.

Deferred minors (polish, not correctness): none open — T11/T12 covered
the polish tasks. UDP resume (re-punch with k_resume) is unimplemented:
sessions over UDP carry k_resume but nothing re-signals after death;
a UDP link death surfaces as LINK_LOST like TCP-expiry. Cost if wrong:
manual redial for UDP games.

Manual real-world NAT matrix (§16) — a human must run ( CI cannot):

- [ ] home → home, no UPnP, no VPN: punch offer/answer connects, game ends
- [ ] same with UPnP on: direct code via mapped port connects
- [ ] VPN → VPN (e.g. Tailscale): direct code to VPN address connects
- [ ] phone hotspot guest: punch fails with the symmetric-NAT message
- [ ] CGNAT host: UPnP reports CGNAT, offers punch/manual only
- [ ] lossy link (`tc netem`): game completes, resume after kill works

Suite command (stdlib only, from repo root; tests are script-style so
`unittest discover` does NOT pick them up — run each file):
`foreach ($f in Get-ChildItem tests/test_online_*.py) { python $f.FullName }`
(pwsh) or `for f in tests/test_online_*.py; do python3 \"$f\"; done` (sh).
Counts at T12: lan_golden 3, crypto_invite 11, framing_tcp 7, handshake 10,
session 6, t6 7, menu 10, t7_stun 11, t8_signal 10, t9_rudp 7, t10_punch 8,
t11_upnp 8, t12_ux 9 — 107 total, all green on 2026-10-10 (Windows,
CPython 3.14).

## Blockers / open questions

- None. Ship state per user: after merge, github tracks exactly LICENSE,
  README.md, battleships.py (tests + notes removed pre-merge).
