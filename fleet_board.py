#!/usr/bin/env python3
"""
FleetView — a live, global board of every Claude Code session across your
terminals, plus proactive macOS notifications.

Data source: `claude agents --json` (the authoritative registry, which DOES
include the interactive sessions you open by hand in each terminal — unlike the
`claude agents` TUI, which only lists background/dispatched sessions).

Every poll it:
  - redraws the board, grouped by project folder, waiting-on-you first
  - watches each session's status and fires a notification on a transition:
        * → waiting          → 💬  "<name> needs you"
        * busy → idle         → ✅  "<name> done"
    (only on transitions observed while running — no startup spam, no re-pings)

Zero-install: pure stdlib, no settings.json changes, no hooks. Run it in a
dedicated terminal:

    python3 fleet_board.py                 # live board, polls every 2s
    python3 fleet_board.py --lang zh       # 中文 UI (else FLEET_LANG / locale; default en)
    python3 fleet_board.py --interval 1    # faster refresh
    python3 fleet_board.py --once          # render one frame and exit
    python3 fleet_board.py jump            # switch tmux to whoever's waiting on you
    python3 fleet_board.py jump <pid>      # switch tmux to a specific session
    python3 fleet_board.py --headless      # notifier only, no board (nohup-able)
    python3 fleet_board.py --sticky        # persistent modal alerts (linger ~60s)
    python3 fleet_board.py --all           # list every idle session (don't fold them)
    python3 fleet_board.py --no-notify     # board only, no notifications
    python3 fleet_board.py --usage         # one-shot detailed usage estimate, then exit
    python3 fleet_board.py --budget 20M    # set your weighted-5h ceiling for the estimate
    python3 fleet_board.py --no-usage      # hide the usage line
    python3 fleet_board.py --test-notify   # fire a sample notification and exit

A usage line shows under the title by default: '📊 now' is an ESTIMATE of how
much you're consuming now (weighted tokens in the rolling 5h window vs your own
busiest 5h, or --budget), plus today's raw token total. It is NOT your real
Pro/Max plan-limit % (that gauge isn't exposed to scripts). See fleet_usage.py.

Notifications lead with the project folder in the title. macOS banners are
short-lived by OS policy; use --sticky for persistent alerts, or set the
notifier app's style to "Alerts" in System Settings to make banners linger.
"""

import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata

# Core i18n (English / 中文). Pick with --lang en|zh, FLEET_LANG, or your locale.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fleet_i18n import t, set_lang_from_argv

# Optional usage tracking (real token throughput from session transcripts).
try:
    from fleet_usage import (UsageTracker, fmt_tok, parse_budget, text_bar,
                             dump, estimate, fmt_dur)
    HAVE_USAGE = True
except Exception:
    HAVE_USAGE = False

# Optional tmux integration (locate each session's pane, and teleport to it).
try:
    import fleet_tmux
    HAVE_TMUX = True
except Exception:
    HAVE_TMUX = False

# Optional session details read from transcripts (what is each session doing?).
try:
    from fleet_detail import Details
    HAVE_DETAIL = True
except Exception:
    HAVE_DETAIL = False

HOME = os.path.expanduser("~")
# Latest board state, written each poll so `fleet jump` answers instantly
# without re-spawning `claude agents --json`.
CACHE_PATH = os.path.join(HOME, ".fleetview", "board.json")

# ── ANSI ──────────────────────────────────────────────────────────────────
RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
GREEN = "\033[32m"; YELLOW = "\033[33m"; CYAN = "\033[36m"
GREY = "\033[90m"; MAGENTA = "\033[35m"; RED = "\033[31m"
HOME_CUR = "\033[H"; CLR_EOL = "\033[K"; CLR_BELOW = "\033[J"
HIDE_CUR = "\033[?25l"; SHOW_CUR = "\033[?25h"
# Mouse reporting (normal + SGR-extended) so board rows are clickable.
MOUSE_ON = "\033[?1000h\033[?1006h"; MOUSE_OFF = "\033[?1006l\033[?1000l"
# Hotkey pool for clickable rows ('q' reserved for quit).
HOTKEYS = "abcdefghijklmnoprstuvwxyz0123456789"

ANSI_RE = re.compile(r"\033\[[0-9;?]*[a-zA-Z]")


def _char_w(ch):
    """Approximate terminal column width of one character (0, 1, or 2)."""
    if unicodedata.combining(ch):
        return 0
    o = ord(ch)
    # CJK / fullwidth and the emoji & pictograph blocks we draw with render wide.
    if (0x1100 <= o <= 0x115F or 0x2329 <= o <= 0x232A or
            0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or
            0xF900 <= o <= 0xFAFF or 0xFE30 <= o <= 0xFE4F or
            0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6 or
            0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF):
        return 2
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def vis_width(s):
    """Visible column width of `s`, ignoring ANSI escape sequences."""
    return sum(_char_w(c) for c in ANSI_RE.sub("", s))


def pad(s, width):
    """Left-justify `s` to `width` *visible* columns (CJK/emoji aware), so a
    translated label keeps the columns after it aligned regardless of language."""
    return s + " " * max(0, width - vis_width(s))


def clip(s, width):
    """Truncate `s` to `width` visible columns, leaving ANSI escapes intact and
    counting wide (CJK/emoji) glyphs as two. Appends RESET so a cut never lets a
    color bleed, and an ellipsis to mark that something was dropped. This is what
    stops a long path/name from wrapping and corrupting the fixed-position redraw
    on a narrow terminal."""
    if width <= 0:
        return ""
    if vis_width(s) <= width:
        return s
    out, col, i, limit = [], 0, 0, width - 1
    while i < len(s):
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = _char_w(s[i])
        if col + w > limit:
            break
        out.append(s[i])
        col += w
        i += 1
    out.append("…" + RESET)
    return "".join(out)

# status → (sort priority, icon, label-key, color). The label is an i18n key
# resolved per-row so the board follows the active language.
STATUS_STYLE = {
    "waiting": (0, "🟡", "status.waiting", YELLOW + BOLD),
    "busy":    (1, "🟢", "status.busy",    CYAN),
    "idle":    (2, "⚪", "status.idle",     GREY),
}
UNKNOWN = (3, "·", None, GREY)


def style_for(status):
    return STATUS_STYLE.get(status, (UNKNOWN[0], UNKNOWN[1], None, UNKNOWN[2]))


def short(p):
    return ("~" + p[len(HOME):]) if p and p.startswith(HOME) else (p or "")


def project_root(cwd):
    """Absolute path of the nearest enclosing .git root, else cwd itself."""
    cur = cwd or ""
    for _ in range(40):
        if cur and os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return (cwd or "").rstrip("/")


def project(cwd):
    root = project_root(cwd)
    return os.path.basename(root) or root or (cwd or "?")


def subdir_tail(cwd):
    """cwd relative to its project root, or '' when it IS the root — so a row can
    show just the './sub/dir' that distinguishes it instead of repeating the
    group's full path."""
    root = project_root(cwd)
    if not cwd or not root:
        return ""
    try:
        rel = os.path.relpath(cwd, root)
    except ValueError:
        return ""
    return "" if rel in (".", "") else rel


def uptime(ms):
    s = int(time.time() - (ms or 0) / 1000)
    if s < 0:
        return "0s"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def get_sessions():
    """Return list of session dicts, or None on failure."""
    try:
        out = subprocess.run(["claude", "agents", "--json"],
                             capture_output=True, text=True, timeout=20)
        return json.loads(out.stdout or "[]")
    except Exception:
        return None


def write_cache(sessions, ts):
    """Persist a slim snapshot (incl. resolved tmux panes) for `fleet jump`."""
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        slim = [{"sessionId": s.get("sessionId"), "pid": s.get("pid"),
                 "status": s.get("status"), "cwd": s.get("cwd"),
                 "pane": s.get("pane")} for s in sessions]
        tmp = CACHE_PATH + f".tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"ts": ts, "sessions": slim}, f)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def read_cache(max_age=10):
    """The running board's last snapshot if it's fresh enough, else None."""
    try:
        with open(CACHE_PATH) as f:
            d = json.load(f)
        if time.time() - d.get("ts", 0) <= max_age:
            return d.get("sessions") or None
    except (OSError, ValueError):
        pass
    return None


def apply_question_status(sessions):
    """`claude agents --json` reports a session that ended its turn with a *text*
    question as plain `idle` — indistinguishable from "done / nothing happening".
    Using the transcript-derived detail, re-surface those as `waiting` so they
    pin to the top, render 🟡, and fire a needs-you ping (not a stray 'done')."""
    for s in sessions:
        det = s.get("detail") or {}
        if s.get("status") == "idle" and det.get("is_question"):
            s["status"] = "waiting"
    return sessions


NOTIFY_STICKY = False  # set by --sticky: persistent modal alerts instead of banners
NOTIFY_COOLDOWN = 90    # min seconds between macOS pops for the same session
MIN_BUSY_FOR_DONE = 20  # ignore busy spells shorter than this for a "done" ping


def notify(title, subtitle, body, sound="Glass"):
    def san(s):
        s = " ".join((s or "").split())
        return s.replace("\\", " ").replace('"', "'")[:180]
    title, subtitle, body = san(title), san(subtitle), san(body)
    if NOTIFY_STICKY:
        # a persistent alert dialog that lingers until dismissed (or 60s)
        args = ["osascript", "-e", "beep",
                "-e", f'display alert "{title}" message "{subtitle} — {body}" '
                      f'giving up after 60']
    else:
        args = ["osascript", "-e",
                f'display notification "{body}" with title "{title}" '
                f'subtitle "{subtitle}" sound name "{sound}"']
    try:
        subprocess.Popen(args, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def display_name(s):
    return s.get("name") or f"pid {s.get('pid', '?')}"


def detect_and_notify(sessions, state, enabled, now=None):
    """Compare current statuses to the previous poll; notify on transitions.

    `state` maps sessionId → {st, since, busy_since, last_notify} and is mutated
    in place. Returns the (time, message) events generated this call.

    Two guards keep the pings calm:
      - a per-session cooldown (NOTIFY_COOLDOWN) so a flapping session can't pop
        the same alert over and over;
      - a minimum busy duration (MIN_BUSY_FOR_DONE) so a brief blink of 'busy'
        doesn't fire a spurious 'done'.
    A session's first sighting only seeds state — it never notifies (no startup
    spam)."""
    now = now if now is not None else time.time()
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    new_events = []
    seen = set()
    for s in sessions:
        sid = s.get("sessionId") or f"pid{s.get('pid')}"
        seen.add(sid)
        st = s.get("status")
        rec = state.get(sid)
        if rec is None:
            state[sid] = {"st": st, "since": now,
                          "busy_since": now if st == "busy" else None,
                          "last_notify": 0.0}
            continue
        prev = rec["st"]
        if st != prev:
            proj = project(s.get("cwd", ""))
            pid = s.get("pid", "?")
            pane = s.get("pane")
            loc = pane["target"] if pane else f"pid {pid}"
            det = s.get("detail") or {}
            question = det.get("last") if det.get("is_question") else None
            # Lead the notification body with where to go, so the banner itself
            # points you at the tmux pane (run `fleet jump` to teleport there).
            detail = f"→ {loc} · {short(s.get('cwd', ''))}"
            tag = f"{GREY}({loc}){RESET}"
            cooled = (now - rec["last_notify"]) >= NOTIFY_COOLDOWN
            if st == "waiting":
                # When it's a text question, put the question itself in the banner.
                body = f"{question}  · {loc}" if question else detail
                if enabled and cooled:
                    notify(f"💬 {proj}", t("notify.waiting"), body, "Ping")
                    rec["last_notify"] = now
                qtag = f"  {DIM}{question}{RESET}" if question else ""
                new_events.append((clock, f"💬 {proj} {t('event.waiting')} {tag}{qtag}"))
            elif st == "idle" and prev == "busy":
                busy_dur = now - (rec["busy_since"] or now)
                if busy_dur >= MIN_BUSY_FOR_DONE:
                    if enabled and cooled:
                        notify(f"✅ {proj}", t("notify.done"), detail, "Glass")
                        rec["last_notify"] = now
                    new_events.append((clock, f"✅ {proj} {t('event.done')} {tag}"))
            rec["st"] = st
            rec["since"] = now
            rec["busy_since"] = now if st == "busy" else None
    # forget sessions that have disappeared
    for sid in list(state):
        if sid not in seen:
            del state[sid]
    return new_events


def usage_line(u, budget=None):
    """One compact line: an ESTIMATE of how much you're consuming right now
    (weighted tokens in the rolling window vs your own busiest window / budget),
    plus today's raw token throughput across the whole fleet."""
    parts = []
    est = estimate(u, budget)
    if est:
        frac = est["pct"]
        color = RED if frac >= 1 else (YELLOW if frac >= 0.8 else GREEN)
        tag = f" {est['headline_model']}" if est.get("headline_model") else ""
        parts.append(f"{color}📊 {t('usage.now')}{tag} [{text_bar(frac, 10)}] "
                     f"~{frac * 100:.0f}%{RESET}")
        # per-model breakdown when more than one family is burning right now
        pm = [p for p in est.get("per_model", []) if p["pct"] >= 0.005]
        if len(pm) > 1:
            seg = " ".join(f"{p['model']} {p['pct'] * 100:.0f}%" for p in pm[:3])
            parts.append(f"{GREY}{seg}{RESET}")
        if est["resets_in"]:
            parts.append(f"{GREY}{t('usage.window', d=fmt_dur(est['resets_in']))}{RESET}")
    fams = " / ".join(f"{m} {fmt_tok(n)}" for m, n in
                      sorted(u["by_model"].items(), key=lambda x: -x[1])[:3])
    parts.append(f"{MAGENTA}💰 {t('usage.today')} {BOLD}{fmt_tok(u['today_total'])}{RESET}"
                 f"{MAGENTA} tok{RESET}")
    if fams:
        parts.append(f"{GREY}{fams}{RESET}")
    return " " + "  ·  ".join(parts)


def session_loc(s):
    """The actionable locator for a session: its tmux target (so you can jump
    there), falling back to the pid when the session isn't inside tmux."""
    pane = s.get("pane")
    if pane:
        return f"{CYAN}{pane['target']:<13}{RESET}"
    return f"{GREY}pid {str(s.get('pid', '?')):<9}{RESET}"


def session_row(s, key=None):
    """One session line: hotkey (when interactive), the tmux pane to jump to,
    uptime, then what the session *is* — Claude's own session title from the
    transcript (falling back to the first prompt, then the subdir tail)."""
    _, icon, lkey, color = style_for(s.get("status"))
    label = t(lkey) if lkey else (s.get("status") or "?")
    up = uptime(s.get("startedAt"))
    name = s.get("name", "")
    ident = (s.get("detail") or {}).get("label")
    tail = subdir_tail(s.get("cwd", ""))
    info = ident or (f"./{tail}" if tail else "")
    info_s = f"{GREY}{info}{RESET}" if info else ""
    lead = f" {BOLD}{YELLOW}{key}{RESET} " if key else "   "
    return (f"{lead}{color}{icon} {pad(label, 10)}{RESET} "
            f"{session_loc(s)} "
            f"{GREY}up {up:>6}{RESET}  "
            f"{(BOLD + name + RESET + '  ') if name else ''}{info_s}".rstrip())


def render(sessions, alerts, width, usage=None, budget=None,
           show_all=False, stale=None, reachable=True, interactive=False):
    """Build the board. Returns (lines, targets, hotkeys):
      - lines:   the rendered, width-clipped text lines
      - targets: a session dict (or None) per line, for click→jump mapping
      - hotkeys: {char: session} for keyboard→jump
    """
    clock = time.strftime("%H:%M:%S")
    lines, targets, hotkeys = [], [], {}
    keypool = iter(HOTKEYS)

    def emit(line, sess=None):
        lines.append(line)
        targets.append(sess)

    def emit_session(s):
        key = None
        if interactive and s.get("pane"):
            key = next(keypool, None)
            if key:
                hotkeys[key] = s
        emit(session_row(s, key), s)
        # Second line for the sessions you act on: what it just said / is asking.
        det = s.get("detail") or {}
        last = det.get("last")
        if last and s.get("status") in ("waiting", "busy"):
            # 💬 only for a real pending question (a surfaced 'waiting'); a busy
            # session is working, so its last line is just context (↳).
            pending = s.get("status") == "waiting" and det.get("is_question")
            mark, col = ("💬", YELLOW) if pending else ("↳", GREY)
            emit(f"        {col}{mark} {last}{RESET}", s)

    def done():
        return [clip(ln, width) for ln in lines], targets, hotkeys

    if sessions is None:
        if reachable:
            emit(f" {BOLD}FleetView{RESET}  {GREY}{t('board.connecting')}{RESET}")
        else:
            emit(f" {BOLD}FleetView{RESET}  {RED}{t('board.unreachable')}{RESET}")
        emit(f" {GREY}{t('board.retrying', clock=clock)}{RESET}")
        return done()

    waiting = sum(1 for s in sessions if s.get("status") == "waiting")
    busy = sum(1 for s in sessions if s.get("status") == "busy")
    idle = sum(1 for s in sessions if s.get("status") == "idle")
    head = (f" {BOLD}FleetView{RESET}  {t('head.sessions', n=len(sessions))}   "
            f"{YELLOW}🟡{waiting} {t('head.waiting')}{RESET}  "
            f"{CYAN}🟢{busy} {t('head.busy')}{RESET}  "
            f"{GREY}⚪{idle} {t('head.idle')}{RESET}")
    if stale:
        head += f"   {YELLOW}⟳ {t('head.stale', n=int(stale))}{RESET}"
    quit_hint = t("hint.jump") if interactive else t("hint.quit")
    head += f"      {GREY}{clock} · {quit_hint}{RESET}"
    emit(head)
    if usage:
        emit(usage_line(usage, budget))
    emit(GREY + "─" * min(width, 80) + RESET)

    if not sessions:
        emit("")
        emit(f"   {GREY}{t('board.none')}{RESET}")
        return done()

    groups = {}
    for s in sessions:
        groups.setdefault(project(s.get("cwd", "?")), []).append(s)

    def grank(items):
        return min(style_for(s.get("status"))[0] for s in items)

    for proj in sorted(groups, key=lambda p: (grank(groups[p]), p.lower())):
        items = sorted(groups[proj], key=lambda s: (style_for(s.get("status"))[0],
                                                     -s.get("startedAt", 0)))
        emit("")
        ptok = (usage or {}).get("by_project", {}).get(proj)
        ann = f"  {CYAN}{fmt_tok(ptok)}{RESET}{GREY} {t('board.today')}{RESET}" if ptok else ""
        root = project_root(items[0].get("cwd", ""))
        emit(f" {BOLD}▸ {proj}{RESET}{ann}  {GREY}{short(root)}{RESET}")

        idle_items = [s for s in items if s.get("status") == "idle"]
        live_items = [s for s in items if s.get("status") != "idle"]
        for s in live_items:
            emit_session(s)
        # Fold a project's idle terminals into one line so stale, long-running
        # idle sessions can't crowd out the rows that actually need you. --all
        # (or a lone idle) expands them.
        if idle_items and not show_all and len(idle_items) > 1:
            locs = ", ".join((s["pane"]["target"] if s.get("pane")
                              else f"pid {s.get('pid', '?')}")
                             for s in idle_items[:6])
            extra = f" +{len(idle_items) - 6}" if len(idle_items) > 6 else ""
            emit(f"   {GREY}⚪ {t('board.idle_fold', n=len(idle_items))}   {locs}{extra}{RESET}")
        else:
            for s in idle_items:
                emit_session(s)

    if alerts:
        emit("")
        emit(f" {GREY}{t('board.alerts')}{RESET}")
        for ts, msg in alerts[-5:]:
            emit(f"   {GREY}{ts}{RESET}  {msg}")
    return done()


def clamp(lines, targets, rows):
    """Trim to the viewport height, adding a '+N more' marker on overflow.
    Keeps `targets` aligned with the trimmed `lines` for click mapping."""
    avail = max(1, rows - 1)
    if len(lines) > avail:
        hidden = len(lines) - (avail - 1)
        lines = lines[:avail - 1] + [
            f"{GREY}{t('board.more', n=hidden)}{RESET}"]
        targets = targets[:avail - 1] + [None]
    return lines, targets


def draw(lines):
    buf = [HOME_CUR]
    for ln in lines:
        buf.append(ln + CLR_EOL + "\n")
    buf.append(CLR_BELOW)
    sys.stdout.write("".join(buf))
    sys.stdout.flush()


def parse_input(s):
    """Decode a chunk of stdin (latin-1 str) into events: ('quit',),
    ('key', ch), or ('click', x, y). Handles SGR (1006) and legacy X10 mouse."""
    events = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in ("\x03", "q", "Q"):       # Ctrl-C / q → quit
            events.append(("quit",)); i += 1; continue
        if c == "\x1b":
            m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", s[i:])
            if m:                          # SGR mouse
                b, x, y, typ = (int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), m.group(4))
                if typ == "M" and (b & 3) == 0:   # left-button press
                    events.append(("click", x, y))
                i += m.end(); continue
            if s[i:i + 3] == "\x1b[M" and i + 6 <= n:   # X10 mouse
                b, x, y = ord(s[i + 3]) - 32, ord(s[i + 4]) - 32, ord(s[i + 5]) - 32
                if (b & 3) == 0:
                    events.append(("click", x, y))
                i += 6; continue
            i += 1; continue               # other escape — skip
        if c.isprintable():
            events.append(("key", c))
        i += 1
    return events


def handle_input(data, targets, hotkeys):
    """Act on a stdin chunk: jump on click/hotkey. Returns True to quit."""
    for ev in parse_input(data):
        if ev[0] == "quit":
            return True
        s = None
        if ev[0] == "key":
            s = hotkeys.get(ev[1])
        elif ev[0] == "click":
            _, _x, y = ev
            if 1 <= y <= len(targets):
                s = targets[y - 1]
        if s and s.get("pane") and HAVE_TMUX:
            fleet_tmux.jump(s["pane"])
    return False


def pick_jump_target(sessions, arg):
    """Choose which session `fleet jump` should teleport to. With an argument,
    match by pid or tmux target/pane-id. Without one, the session waiting on you
    (or the only one, if there's a single session). None if nothing fits."""
    if arg:
        for s in sessions:
            if str(s.get("pid")) == str(arg):
                return s
        for s in sessions:
            pane = s.get("pane") or {}
            if arg in (pane.get("target", ""), pane.get("pane_id", "")):
                return s
        return None
    waiting = sorted((s for s in sessions if s.get("status") == "waiting"),
                     key=lambda s: str(s.get("pid")))
    if waiting:
        return waiting[0]
    return sessions[0] if len(sessions) == 1 else None


def do_jump(args):
    """`fleet jump [pid|target]` — switch the tmux client to a session's pane."""
    if not HAVE_TMUX:
        print("tmux integration unavailable (fleet_tmux.py missing).")
        return
    sessions = read_cache()  # instant if a board is running
    if sessions is None:
        sessions = get_sessions() or []
        fleet_tmux.resolve(sessions)
    if not sessions:
        print("no live Claude Code sessions.")
        return

    arg = args[0] if args else None
    target = pick_jump_target(sessions, arg)
    if target is None:
        if arg:
            print(f"no session matches '{arg}'. live sessions:")
        else:
            print("nobody is waiting on you — pick one:  fleet jump <pid>")
        for s in sessions:
            pane = s.get("pane")
            loc = pane["target"] if pane else "(not in tmux)"
            print(f"  {s.get('status', '?'):8} pid {str(s.get('pid', '?')):<6} "
                  f"{loc:<14} {short(s.get('cwd', ''))}")
        return

    pane = target.get("pane")
    if not pane:
        print(f"that session (pid {target.get('pid')}) isn't in a tmux pane "
              f"— can't jump.")
        return
    ok, mode = fleet_tmux.jump(pane)
    if not ok:
        print(f"jump failed: {mode}")
    elif mode == "focus":
        print(f"→ focus → {pane['target']}  (same session, view unchanged)")
    elif mode == "tab":
        print(f"→ brought {pane['session']}'s terminal tab to front  ({pane['target']})")
    else:
        print(f"→ switched to {pane['target']} in place  (prefix+L to come back)")


class Poller(threading.Thread):
    """Polls `claude agents --json` on a background thread so a slow or hung
    `claude` (the call can block up to its 20s timeout) can never freeze the
    board. The render loop just reads the latest published snapshot; transition
    detection and the usage refresh run here, exactly once per successful poll."""

    def __init__(self, interval, notify_on, tracker):
        super().__init__(daemon=True)
        self.interval = interval
        self.notify_on = notify_on
        self.tracker = tracker
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.state = {}
        self.alerts = []
        self.sessions = None
        self.ts = 0.0          # epoch of last successful poll (0 = none yet)
        self.reachable = True
        self.details = Details() if HAVE_DETAIL else None

    def run(self):
        while not self._stop.is_set():
            s = get_sessions()
            now = time.time()
            if s is not None:
                if HAVE_TMUX:
                    fleet_tmux.resolve(s)       # attach each session's tmux pane
                if self.details is not None:
                    self.details.annotate(s)    # topic / last line / is_question
                    apply_question_status(s)    # text questions → needs-you
            with self._lock:
                if s is not None:
                    self.alerts.extend(
                        detect_and_notify(s, self.state, self.notify_on, now))
                    del self.alerts[:-8]
                    self.sessions = s
                    self.ts = now
                    self.reachable = True
                    write_cache(s, now)
                else:
                    self.reachable = False
                if self.tracker:
                    self.tracker.refresh()  # internally throttled
            self._stop.wait(self.interval)

    def render_data(self):
        """Latest (sessions, alerts, ts, reachable, usage-snapshot), under lock."""
        with self._lock:
            snap = self.tracker.snapshot() if self.tracker else None
            return (self.sessions, list(self.alerts), self.ts,
                    self.reachable, snap)

    def stop(self):
        self._stop.set()


def main():
    global NOTIFY_STICKY
    argv = sys.argv[1:]
    set_lang_from_argv(argv)   # honor --lang en|zh (else FLEET_LANG / locale)

    # `fleet jump [pid|target]` — teleport to a session's tmux pane, then exit.
    if argv and argv[0] == "jump":
        do_jump(argv[1:])
        return

    interval = 2.0
    notify_on = True
    once = False
    NOTIFY_STICKY = "--sticky" in argv

    # usage tracking (real token throughput from session transcripts)
    usage_on = HAVE_USAGE and "--no-usage" not in argv
    budget = None
    if HAVE_USAGE and "--budget" in argv:
        try:
            budget = parse_budget(argv[argv.index("--budget") + 1])
        except IndexError:
            pass

    if "--test-notify" in argv:
        notify(t("test.title"), t("notify.waiting"), t("test.body"), "Ping")
        print(t("test.sent"))
        print(t("test.allow"))
        print(t("test.linger"))
        return
    if "--usage" in argv:
        if not HAVE_USAGE:
            print("usage tracking unavailable (fleet_usage.py not found alongside fleet_board.py)")
            return
        hours = 5
        if "--window" in argv:
            try:
                hours = float(argv[argv.index("--window") + 1])
            except (ValueError, IndexError):
                pass
        dump(hours=hours, budget=budget)
        return
    if "--once" in argv:
        once = True
    if "--no-notify" in argv:
        notify_on = False
    if "--interval" in argv:
        try:
            interval = float(argv[argv.index("--interval") + 1])
        except Exception:
            pass

    headless = "--headless" in argv
    show_all = "--all" in argv
    state = {}
    alerts = []
    tracker = UsageTracker() if usage_on else None
    details = Details() if HAVE_DETAIL else None

    def enrich(sessions):
        """Attach tmux panes + transcript detail, and surface text questions."""
        if sessions is None:
            return
        if HAVE_TMUX:
            fleet_tmux.resolve(sessions)
        if details is not None:
            details.annotate(sessions)
            apply_question_status(sessions)

    if once:
        sessions = get_sessions()
        enrich(sessions)
        if sessions is not None:
            write_cache(sessions, time.time())
        # seed state so a one-shot doesn't claim transitions
        detect_and_notify(sessions or [], state, False)
        if tracker:
            tracker.refresh(force=True)
        size = shutil.get_terminal_size((90, 40))
        snap = tracker.snapshot() if tracker else None
        lines, _, _ = render(sessions, alerts, size.columns, snap, budget,
                             show_all, reachable=sessions is not None)
        for ln in lines:
            print(ln)
        return

    if headless:
        print(f"FleetView notifier running (poll {interval}s, "
              f"notify={'on' if notify_on else 'off'}). Ctrl-C to stop.", flush=True)
        try:
            while True:
                sessions = get_sessions()
                if sessions is not None:
                    enrich(sessions)
                    write_cache(sessions, time.time())
                    for ts, msg in detect_and_notify(sessions, state, notify_on):
                        print(f"{ts}  {msg}", flush=True)
                time.sleep(interval)
        except KeyboardInterrupt:
            pass
        return

    # Interactive board: poll on a background thread so a slow/hung `claude`
    # can't freeze the UI; the loop here renders the latest snapshot and turns
    # clicks / hotkeys into tmux jumps.
    poller = Poller(interval, notify_on, tracker)
    poller.start()
    interactive = sys.stdin.isatty() and HAVE_TMUX
    fd = sys.stdin.fileno()
    old_term = None
    try:
        if interactive:
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            sys.stdout.write(MOUSE_ON)
        sys.stdout.write(HIDE_CUR)
        sys.stdout.flush()
        while True:
            sessions, alerts, ts, reachable, snap = poller.render_data()
            now = time.time()
            stale = (now - ts) if (ts and now - ts > 2 * interval) else None
            size = shutil.get_terminal_size((90, 40))
            lines, targets, hotkeys = render(sessions, alerts, size.columns,
                                             snap, budget, show_all, stale,
                                             reachable, interactive)
            lines, targets = clamp(lines, targets, size.lines)
            draw(lines)
            if interactive:
                r, _, _ = select.select([sys.stdin], [], [], interval)
                if r and handle_input(os.read(fd, 4096).decode("latin-1", "ignore"),
                                      targets, hotkeys):
                    break
            else:
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        if interactive and old_term is not None:
            sys.stdout.write(MOUSE_OFF)
            termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
        sys.stdout.write(SHOW_CUR + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
