#!/usr/bin/env python3
"""
FleetView ↔ tmux — locate each Claude Code session's tmux pane, and teleport to
it.

Zero-install, pure stdlib: a session's pid (from `claude agents --json`) is the
`claude` process; its tmux pane is found by walking the process tree up until an
ancestor matches a pane's shell pid (`#{pane_pid}`). So no hook, no env var, no
per-terminal setup is required — it just works for the interactive sessions you
open by hand, as long as they're inside tmux.

    fleet jump            # switch to the session that's waiting on you
    fleet jump <pid>      # switch to a specific session (by pid or tmux target)
"""

import os
import subprocess
import sys


def _run(args, timeout=5):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def parent_map():
    """pid -> ppid for every process (BSD/macOS & Linux `ps` both support this)."""
    parents = {}
    for line in _run(["ps", "-axo", "pid=,ppid="]).splitlines():
        f = line.split()
        if len(f) >= 2:
            try:
                parents[int(f[0])] = int(f[1])
            except ValueError:
                pass
    return parents


def pane_map():
    """shell-pid -> pane info for every tmux pane across all sessions/clients.
    Empty dict if tmux isn't running. Each value:
        {'pane_id': '%5', 'session': 'os', 'target': 'os:1.1', 'window_name': …}
    """
    fmt = ("#{pane_pid}\t#{pane_id}\t#{session_name}\t#{window_index}\t"
           "#{pane_index}\t#{window_name}")
    panes = {}
    for line in _run(["tmux", "list-panes", "-a", "-F", fmt]).splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        try:
            ppid = int(f[0])
        except ValueError:
            continue
        panes[ppid] = {
            "pane_id": f[1],
            "session": f[2],
            "target": f"{f[2]}:{f[3]}.{f[4]}",
            "window_name": f[5] if len(f) > 5 else "",
        }
    return panes


def pane_for_pid(pid, panes, parents, max_hops=40):
    """Walk pid's ancestry until it hits a pane's shell pid. None if not in tmux."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    hops = 0
    while pid and pid > 1 and hops < max_hops:
        if pid in panes:
            return panes[pid]
        pid = parents.get(pid)
        hops += 1
    return None


def resolve(sessions):
    """Attach a 'pane' dict (or None) to each session dict, in place. One `ps`
    and one `tmux list-panes` call total, regardless of session count."""
    panes = pane_map()
    parents = parent_map() if panes else {}
    for s in sessions:
        s["pane"] = pane_for_pid(s.get("pid"), panes, parents) if panes else None
    return sessions


def current_session():
    """Name of the tmux session the invoking client is attached to, or None.
    Uses $TMUX_PANE (set both for a normal shell inside tmux and for a key
    binding's run-shell) so it's deterministic rather than guessing a client."""
    pane = os.environ.get("TMUX_PANE")
    args = ["tmux", "display-message", "-p", "#{session_name}"]
    if pane:
        args[2:2] = ["-t", pane]
    return _run(args).strip() or None


def clients_for_session(session):
    """TTYs of every tmux client (terminal tab/window) attached to `session`."""
    out = _run(["tmux", "list-clients", "-t", session, "-F", "#{client_tty}"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _activate_terminal_tab(tty):
    """Bring the macOS Terminal.app tab living on `tty` to the front, leaving
    every other tab (e.g. the board's own) where it is. True if it worked."""
    if sys.platform != "darwin" or not tty:
        return False
    tty = tty.replace('"', "")
    script = ('tell application "Terminal"\n'
              '  repeat with w in windows\n'
              '    repeat with t in tabs of w\n'
              '      try\n'
              f'        if tty of t is "{tty}" then\n'
              '          set selected of t to true\n'
              '          set index of w to 1\n'
              '          activate\n'
              '          return "ok"\n'
              '        end if\n'
              '      end try\n'
              '    end repeat\n'
              '  end repeat\n'
              'end tell\n'
              'return "notfound"')
    return _run(["osascript", "-e", script]).strip() == "ok"


def jump(pane):
    """Move focus to `pane` (a pane dict, or a '%N' id), preferring to switch to
    the terminal tab that's ALREADY showing the target session over hijacking
    your current tab. Order of preference:

      - same session as you  → just select the pane (layout untouched) → 'focus'
      - another session that has its own terminal tab → activate that tab → 'tab'
      - target session has no attached tab → switch-client in place → 'switch'
        (or 'select' when there's no client at all, e.g. outside tmux)

    Returns (ok, mode) or (False, error)."""
    pane_id = pane.get("pane_id") if isinstance(pane, dict) else pane
    target_session = pane.get("session") if isinstance(pane, dict) else None
    if not pane_id:
        return False, "no pane id"

    cur = current_session()
    # Always set the active pane/window inside the target's own session, so
    # whichever client shows it lands on the right pane.
    _run(["tmux", "select-window", "-t", pane_id])
    _run(["tmux", "select-pane", "-t", pane_id])

    if target_session and cur and target_session == cur:
        return True, "focus"   # already in this session's tab

    # Prefer the existing terminal tab attached to the target session.
    if target_session:
        for tty in clients_for_session(target_session):
            if _activate_terminal_tab(tty):
                return True, "tab"

    # No attached tab (or activation unavailable): fall back to in-place switch.
    try:
        r = subprocess.run(["tmux", "switch-client", "-t", pane_id],
                           capture_output=True, text=True, timeout=5)
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        return (True, "select") if "no current client" in err.lower() \
            else (False, err or "switch failed")
    return True, "switch"
