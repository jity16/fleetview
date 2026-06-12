#!/usr/bin/env python3
"""
One-shot board snapshot built from `claude agents --json` (the authoritative
session registry). Renders every live Claude Code session, grouped by project.

Unlike the interactive `claude agents` TUI it needs no TTY, so it's handy for a
quick glance. Note: --json only exposes busy/idle; the real agent view also
shows each session's last response and whether it's waiting on you.
"""
import json
import os
import subprocess
import sys
import time

from fleet_i18n import t, set_lang_from_argv

HOME = os.path.expanduser("~")


def short(p):
    return ("~" + p[len(HOME):]) if p.startswith(HOME) else p


def project(cwd):
    cur = cwd
    for _ in range(40):
        if os.path.isdir(os.path.join(cur, ".git")):
            return os.path.basename(cur) or cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.basename(cwd.rstrip("/")) or cwd


def uptime(ms):
    s = int(time.time() - ms / 1000)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600}h"


def main():
    set_lang_from_argv(sys.argv[1:])
    try:
        out = subprocess.run(["claude", "agents", "--json"],
                             capture_output=True, text=True, timeout=20)
        data = json.loads(out.stdout or "[]")
    except Exception as e:
        print(f"  could not read sessions: {e}")
        return

    groups = {}
    for s in data:
        groups.setdefault(project(s.get("cwd", "?")), []).append(s)

    busy = sum(1 for s in data if s.get("status") == "busy")
    icon = {"busy": "● working", "idle": "◐ idle   "}

    print()
    print(f"  Claude Code · agent view (snapshot)     "
          f"{len(data)} sessions · {busy} busy · {len(data) - busy} idle")
    print("  " + "─" * 76)
    for p in sorted(groups, key=str.lower):
        print(f"\n  ▸ {p}")
        for s in sorted(groups[p], key=lambda x: x.get("startedAt", 0)):
            st = icon.get(s.get("status"), s.get("status", "?"))
            up = uptime(s.get("startedAt", time.time() * 1000))
            print(f"     {st:<11} up {up:>6}  pid {str(s.get('pid','?')):<6} "
                  f"{short(s.get('cwd',''))}")
    print()
    print("  ↑↓ select · Space peek · Enter attach · Esc shell   "
          "(real keys in `claude agents`)")
    print(f"  {t('snapshot.note')}")


if __name__ == "__main__":
    main()
