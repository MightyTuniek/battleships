# Epic Game Feel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cinematic impact + persistent battle atmosphere + captain personality to terminal Battleships without changing rules.

**Architecture:** Local-only render helpers in the single file, all gated by new `VISUAL` flags + existing `can_animate()`; `epic_mode` master switches timing table from snappy to Hollywood. No LAN protocol change, no new threads, no dependencies.

**Tech Stack:** Python 3.8+ stdlib only (`time`, `random`), existing ANSI helpers (`paint`, `_burst_frames`, `_burst_print`, `big_banner`).

**Spec:** `docs/superpowers/specs/2026-10-07-epic-game-feel-design.md`

## Global Constraints

- Single-file `battleships.py`, zero-dependency (stdlib + ANSI only).
- Snappy by default (~0.3s hit, ~1.0s sink); `epic_mode` OFF by default allows 1.5–2.0s Hollywood timings.
- Every new effect wrapped in `try/except: pass` and skipped when `not can_animate()`.
- Non-TTY / `--no-color` falls back to current text behavior; no sleeps >0.05s off-TTY.
- V1 kill-cam runs to completion (no input polling), capped by timings table.

## Review Focus

- Non-TTY pipe (`echo B7 | python3 battleships.py --no-color`) must not hang on sleeps or crash on ANSI.
- `epic_mode` OFF must keep a full solo game under current pacing + ≤1s per sink.
- `visual_settings_menu` Reset restores all 8 new flags to spec defaults.
- Taunt path never blocks, never shows twice per turn, never leaks ship positions.
- `damage_fire` flicker must not change `Board`/`Knowledge` state or break `render_boards` width alignment.

---

### Task 1: Settings + timing table

**Files:**
- Modify: `battleships.py:394-410` (`VISUAL_DEFAULTS`, `_VISUAL_SETTINGS` compat)
- Modify: `battleships.py:532-571` (`visual_settings_menu`)
- Test: headless `python3 -c` import checks (no new test file; zero-dep repo has no pytest)

**Interfaces:**
- Consumes: `VISUAL_DEFAULTS`, `_VISUAL_SETTINGS.get/toggle/reset`, `can_animate()`
- Produces: `vis("screen_shake"|"hit_stop"|"kill_cam"|"damage_fire"|"sonar_sweep"|"captain_taunts"|"streaks"|"epic_mode") -> bool`; `EPIC_TIMINGS: dict` consumed by Tasks 2–3

- [ ] **Step 1: Write the failing check**

```python
import battleships as B
for k in ["screen_shake","hit_stop","kill_cam","damage_fire","sonar_sweep","captain_taunts","streaks","epic_mode"]:
    assert k in B.VISUAL_DEFAULTS, k
    assert B.vis(k) == B.VISUAL_DEFAULTS[k], k
assert B.VISUAL_DEFAULTS["epic_mode"] is False
print("settings ok")
```

- [ ] **Step 2: Run check to verify it fails**

Run: `python3 -c 'import battleships as B; assert "screen_shake" in B.VISUAL_DEFAULTS'`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Write minimal implementation**

```python
VISUAL_DEFAULTS = {
    # ... keep all existing keys verbatim ...
    "screen_shake": True,
    "hit_stop": True,
    "kill_cam": True,
    "damage_fire": True,
    "sonar_sweep": True,
    "captain_taunts": True,
    "streaks": True,
    "epic_mode": False,
}
EPIC_TIMINGS = {
    False: {"hit_stop": 0.12, "sunk_stop": 0.25, "shake_frames": 3},
    True: {"hit_stop": 0.30, "sunk_stop": 0.80, "shake_frames": 6},
}
```

In `visual_settings_menu()` items list append (keep existing 13 rows first):

```python
("Screen shake on hits", "screen_shake"),
("Hit-stop pause", "hit_stop"),
("Sunk kill-cam", "kill_cam"),
("Burning damaged ships", "damage_fire"),
("Sonar sweep on enemy turn", "sonar_sweep"),
("Captain taunts", "captain_taunts"),
("Streak callouts", "streaks"),
("EPIC MODE (all Hollywood, slower)", "epic_mode"),
```

- [ ] **Step 4: Run check to verify it passes**

Run: `python3 -m py_compile battleships.py && python3 -c 'import battleships as B; print(sorted(B.VISUAL_DEFAULTS.items()))'`
Expected: PASS, 8 new keys present, `epic_mode False`

- [ ] **Step 5: Commit**

```bash
git add battleships.py
git commit -m "feat: add epic game-feel visual flags and epic timing table"
```

### Task 2: Impact core — hit-stop + shake + kill-cam

**Files:**
- Modify: `battleships.py:447-462` (add `_screen_shake`, `_hit_stop` helpers next to `_burst_frames`)
- Modify: `battleships.py:503-518` (`show_sunk_reveal` letter-by-letter + obituary)
- Modify: `battleships.py:759-803` (`burst_shot`) and `battleships.py:805-848` (`burst_shot_lan`) to call helpers
- Test: headless `python3 -c` checks

**Interfaces:**
- Consumes: `vis()`, `can_animate()`, `EPIC_TIMINGS` from Task 1, `big_banner()`, `render_boards()`
- Produces: `_hit_stop(sunk: bool) -> None`; `_screen_shake(intensity: int) -> None`; upgraded `show_sunk_reveal(player, enemy, ship_name, last_player=None) -> None` consumed by Task 5 war flow (unchanged signature)

- [ ] **Step 1: Write the failing check**

```python
import battleships as B
assert callable(getattr(B, "_hit_stop", None)), "missing _hit_stop"
assert callable(getattr(B, "_screen_shake", None)), "missing _screen_shake"
import inspect
src = inspect.getsource(B.show_sunk_reveal)
assert "S U N K" in src or "letter" in src.lower(), "kill-cam not upgraded"
print("impact ok")
```

- [ ] **Step 2: Run check to verify it fails**

Run: `python3 -c 'import battleships as B; assert callable(getattr(B, "_hit_stop", None))'`
Expected: FAIL with `AssertionError: missing _hit_stop`

- [ ] **Step 3: Write minimal implementation**

```python
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
```

In `burst_shot` / `burst_shot_lan`, first two lines of body become:

```python
_hit_stop(bool(sunk or sunk_len))
_screen_shake(2 if (sunk or sunk_len) else 1)
```

`show_sunk_reveal` upgrade (keep signature, keep early-return guard, insert after `print(render_boards(...))`):

```python
word = "S U N K"
step = ""
for ch in word.split():
    step = (step + " " + ch).strip()
    try:
        if can_animate():
            sys.stdout.write("\r  " + paint(step, "green", "bold"))
            sys.stdout.flush()
            time.sleep(0.15 if vis("epic_mode") else 0.06)
    except Exception:
        pass
print()
cells = enemy.ship_cells.get(ship_name) or []
print("  " + paint("enemy %s destroyed — %d shots to kill" % (ship_name, len(cells)), "green"))
```

- [ ] **Step 4: Run check to verify it passes**

Run: `python3 -m py_compile battleships.py && python3 -c 'import battleships as B; B._VISUAL_SETTINGS.flags["animations"]=False; B._hit_stop(True); B._screen_shake(1); print("no-crash ok")'`
Expected: PASS (guards return instantly when `can_animate()` is False; patch flags if your harness TTY differs)

- [ ] **Step 5: Commit**

```bash
git add battleships.py
git commit -m "feat: add hit-stop, screen shake, and kill-cam sunk reveal"
```

### Task 3: Persistent battle state — fire, sonar, heartbeat

**Files:**
- Modify: `battleships.py:2040-2077` (`own_char`, `track_char` flicker branch)
- Modify: `battleships.py:693-740` (`Spinner.__enter__` sonar ping before thread start)
- Modify: `battleships.py:759-803` (`burst_shot` heartbeat tail)
- Test: headless `python3 -c` checks

**Interfaces:**
- Consumes: `vis()`, `can_animate()`, `paint()`, `burst_banner()` from Tasks 1–2
- Produces: fire rendering inside `own_char/track_char` (no new signature); sonar inside `Spinner`; heartbeat inside `burst_shot` (no new API)

- [ ] **Step 1: Write the failing check**

```python
import battleships as B, inspect
assert "damage_fire" in inspect.getsource(B.own_char) or "flicker" in inspect.getsource(B.own_char).lower()
assert "sonar" in inspect.getsource(B.Spinner.__enter__).lower() or "((" in inspect.getsource(B.Spinner.__enter__)
print("atmosphere ok")
```

- [ ] **Step 2: Run check to verify it fails**

Run: `python3 -c 'import battleships as B, inspect; assert "damage_fire" in inspect.getsource(B.own_char)'`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Write minimal implementation**

In `own_char`, after computing `ch` for `ship and shot`, insert (burning marker, no state change):

```python
if ship and shot and vis("damage_fire"):
    frame = int(time.time() * 4) % 2
    ch = paint("!", "red", "bold") if frame else paint("*", "yellow", "bold")
    if last == (r, c):
        ch = _highlight(ch)
    return ch
```

In `track_char` (signature already has `enemy`, so `is_sunk` is available), insert before the `elif reveal and ship:` branch:

```python
if ship and (r, c) in enemy.shots and not enemy.is_sunk(ship) and vis("damage_fire"):
    frame = int(time.time() * 4) % 2
    ch = paint("!", "red", "bold") if frame else paint("*", "yellow", "bold")
    if last == (r, c):
        ch = _highlight(ch)
    return ch
```

Sonar in `Spinner.__enter__`, right after the `ENEMY TURN` banner block, before thread start:

```python
try:
    if vis("sonar_sweep") and supports_cursor_ui():
        for f in ["((.))", "((o))", "(((o)))"]:
            self.stream.write("\r  sonar " + f)
            self.stream.flush()
            time.sleep(0.07)
        self.stream.write("\r" + " " * 20 + "\r")
        self.stream.flush()
except Exception:
    pass
```

Heartbeat at end of `burst_shot` (after `_bell(...)` line). Spec §3.2 describes an interval scaled by sunk count, but `burst_shot` receives no board — V1 uses a fixed short double-pulse so pacing stays snappy (deviation logged, no signature change):

```python
try:
    if sunk and (vis("terminal_bell") or vis("epic_mode")) and sys.stdout.isatty():
        time.sleep(0.15 if vis("epic_mode") else 0.08)
        _bell("hit")
except Exception:
    pass
```

- [ ] **Step 4: Run check to verify it passes**

Run: `python3 -m py_compile battleships.py && python3 -c 'import battleships as B; b=B.Board(); b.place("Destroyer", [(0,0),(0,1)]); b.fire((0,0)); print(repr(B.own_char(b,0,0))); print("fire ok")'`
Expected: PASS, `own_char` returns flicker `*`/`!` variant (or legacy `X` when flag off — toggle `B._VISUAL_SETTINGS.flags["damage_fire"]=False` and re-run to confirm fallback)

- [ ] **Step 5: Commit**

```bash
git add battleships.py
git commit -m "feat: add damage fire, sonar sweep, and heartbeat"
```

### Task 4: Personality — taunts + streaks + war-report sparkline

**Files:**
- Modify: `battleships.py:144-167` (`GameStats`: add `streak: int = 0` field + include in `as_dict`/`from_dict`)
- Modify: `battleships.py:759-803` (`burst_shot`: streak update + taunt line; keep signature)
- Modify: `battleships.py:1631-1632` (`par_line`) + `battleships.py:2916-2947` (`Game.finish` summary block: append sparkline + MVP)
- Test: headless `python3 -c` checks

**Interfaces:**
- Consumes: `_burst_print()`, `cell_name()`, `Game.shot_history`, `Board.shots/order` from earlier tasks
- Produces: `TAUNTS: Dict[str, Dict[str, List[str]]]`; `GameStats.streak`; `war_sparkline(history) -> str`; `mvp_line(game) -> str`

- [ ] **Step 1: Write the failing check**

```python
import battleships as B
assert hasattr(B, "TAUNTS") and "Easy" in B.TAUNTS and "Nightmare" in B.TAUNTS
for lvl, pools in B.TAUNTS.items():
    total = sum(len(v) for v in pools.values())
    assert total >= 10, (lvl, total)
g = B.GameStats()
assert hasattr(g, "streak")
assert callable(getattr(B, "war_sparkline", None))
print("personality ok")
```

- [ ] **Step 2: Run check to verify it fails**

Run: `python3 -c 'import battleships as B; assert hasattr(B, "TAUNTS")'`
Expected: FAIL with `AssertionError`

- [ ] **Step 3: Write minimal implementation**

```python
# 10 lines per level: 3 hit + 3 miss + 2 sunk + 2 losing. Short, naval, no profanity, no position leaks.
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
```

`GameStats` diff: add `streak: int = 0`, extend `as_dict` with `"streak": self.streak`, extend `from_dict` tuple with `"streak"`.

In `Game._record_shot_history`, first line of body becomes (keeps history write, adds streak; old saves without `streak` default to 0 via `from_dict`'s `d.get(k, 0)`):

```python
self._stats_obj.streak = self._stats_obj.streak + 1 if hit else 0
```

In `burst_shot`, after the existing hit/miss `_burst_print` block, append (guarded, max 1 line, no position leak):

Change signatures (backward compatible — existing callers keep working):

```python
def burst_shot(pos, hit, ship, sunk, opp=False, level="Easy"):
def burst_shot_lan(pos, hit, sunk_len, opp=False, level="Easy"):
```

In `Game.run` normal + salvo paths, pass `level=self.level` at each `burst_shot(...)` call site. In `burst_shot`, after the existing hit/miss `_burst_print` block, append:

```python
_BURST_STREAK = {"n": 0}
# ... inside burst_shot body, after result known:
try:
    _BURST_STREAK["n"] = _BURST_STREAK["n"] + 1 if (hit and not opp) else 0
    if vis("streaks") and not opp and _BURST_STREAK["n"] == 2:
        _burst_print("DOUBLE HIT!", ("yellow", "bold"), hold=0.6)
    elif vis("streaks") and not opp and _BURST_STREAK["n"] >= 3:
        _burst_print("ON FIRE! x%d" % _BURST_STREAK["n"], ("red", "bold"), hold=0.6)
    if vis("captain_taunts") and random.random() < 0.35:
        pool = TAUNTS.get(level, TAUNTS["Easy"]).get("sunk" if sunk else ("hit" if hit else "miss"), [])
        if pool:
            _burst_print("ENEMY CAPTAIN: %s" % random.choice(pool), ("white",), hold=0.5)
except Exception:
    pass
```
In `Game.finish`, after `par_line` print (won branch) and inside summary list, append:

```python
"War: %s" % war_sparkline(self.shot_history),
mvp_line(self),
```

- [ ] **Step 4: Run check to verify it passes**

Run: `python3 -m py_compile battleships.py && python3 -c 'import battleships as B; print(B.war_sparkline([{"hit":True},{"hit":False}])); print(B.mvp_line(type("G",(),{"shot_history":[{"pos":(0,1),"sunk":True,"ship":"Cruiser"}]})()))'`
Expected: PASS, prints `H.` and `MVP: B1 (sunk Cruiser)`

- [ ] **Step 5: Commit**

```bash
git add battleships.py
git commit -m "feat: add captain taunts, streaks, and war-report sparkline"
```

### Task 5: Final verification — pacing, fallback, bench

**Files:**
- Modify: none (verification only; fix fallout in owning task if found)
- Test: full project gates

**Interfaces:**
- Consumes: all tasks above
- Produces: green gates, manual TTY checklist result

- [ ] **Step 1: Syntax gate**

Run: `python3 -m py_compile battleships.py`
Expected: PASS (no output)

- [ ] **Step 2: Headless bench gate (visuals disabled headless)**

Run: `python3 battleships.py --bench 5 --seed 0`
Expected: PASS, prints median + round-robin, no traceback

- [ ] **Step 3: Non-TTY fallback gate**

Run: `printf 'B7\nquit\n' | python3 battleships.py --no-color 2>&1 | head -30`
Expected: PASS, game starts, no hang, no ANSI-only crash

- [ ] **Step 4: Settings toggle gate**

```python
import battleships as B, time
for k in ["screen_shake","hit_stop","kill_cam","damage_fire","sonar_sweep","captain_taunts","streaks"]:
    B._VISUAL_SETTINGS.flags[k] = False
B._hit_stop(True); B._screen_shake(2)
print("toggles-off no-crash ok")
B._VISUAL_SETTINGS.reset()
t0 = time.time(); B._hit_stop(False); dt = time.time() - t0
assert dt < 0.5, dt
print("pacing ok: %.3fs" % dt)
```

Run: `python3 -c '<above>'`
Expected: PASS

- [ ] **Step 5: Commit (only if fixes were needed; else empty)**

```bash
git status --short
# if clean: no commit. If fixes: git add battleships.py && git commit -m "fix: epic feel fallback"
```

Manual TTY checklist (human, 5 min, not automatable): solo Normal 1 game — confirm hit shake ≤0.5s, sunk kill-cam ~1s + obituary, burning `*` on damaged ships, 1 sonar ping on enemy turn, ≤1 taunt/turn, DOUBLE HIT on 2-in-a-row, Settings toggles kill all new effects, EPIC MODE slower but ≤2s per pause.
