#!/usr/bin/env python3
"""
FleetView hook handler.

Invoked by Claude Code hooks. Reads the hook event JSON from stdin, updates a
per-session state file under ~/.fleetview/sessions/<session_id>.json, and fires
a macOS notification on the two moments that matter:
    - Claude is waiting on you / wants to discuss   (Notification, or a Stop
      whose last message looks like a question)
    - Claude finished the turn                        (Stop, non-question)

Working progress is recorded silently (no notification) so the board stays live
without pinging you for every tool call.

Pure stdlib. Always exits 0 so it can never block a Claude turn.
"""

import json
import os
import re
import subprocess
import sys
import time

# i18n is best-effort here: the hook must never fail to import (it can't block a
# Claude turn), so fall back to English if the catalog isn't alongside it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from fleet_i18n import t
except Exception:
    def t(key, **kw):
        _EN = {"hook.needs_you": "{name} needs you",
               "hook.discuss": "{name} wants to discuss",
               "hook.done": "{name} done"}
        s = _EN.get(key, key)
        return s.format(**kw) if kw else s

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".fleetview", "sessions")

# states
WORKING = "working"
NEEDS_YOU = "needs_you"
AWAITING = "awaiting"   # turn ended; substate "question" or "done"
IDLE = "idle"

# How long after a notification before idle re-pings are allowed again (s).
RENOTIFY_COOLDOWN = 100


def log_debug(msg):
    if os.environ.get("FLEET_DEBUG"):
        try:
            with open(os.path.join(HOME, ".fleetview", "debug.log"), "a") as f:
                f.write(f"{time.time():.0f} {msg}\n")
        except Exception:
            pass


def read_payload():
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def project_name(cwd):
    """Default classification: the project folder. Walk up for a .git root for a
    stable name, else use the basename of cwd."""
    if not cwd:
        return "?"
    cur = cwd
    for _ in range(40):
        if os.path.isdir(os.path.join(cur, ".git")):
            return os.path.basename(cur) or cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.basename(cwd.rstrip("/")) or cwd


def short_path(p):
    if p and p.startswith(HOME):
        return "~" + p[len(HOME):]
    return p or ""


def describe_tool(tool_name, tool_input):
    ti = tool_input or {}

    def base(key="file_path"):
        return os.path.basename(str(ti.get(key, ""))) or "?"

    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return f"✏️  editing {base()}"
    if tool_name in ("MultiEdit",):
        return f"✏️  editing {base()}"
    if tool_name == "Read":
        return f"\U0001f4d6 reading {base()}"
    if tool_name == "Bash":
        cmd = re.sub(r"\s+", " ", str(ti.get("command", ""))).strip()
        return f"$ {cmd[:60]}"
    if tool_name in ("Grep", "Glob"):
        pat = str(ti.get("pattern", ti.get("query", "")))[:40]
        return f"\U0001f50e searching {pat}"
    if tool_name in ("Task", "Agent"):
        d = str(ti.get("description", ti.get("subagent_type", "")))[:40]
        return f"\U0001f916 subagent: {d}"
    if tool_name in ("WebFetch", "WebSearch"):
        return "\U0001f310 web lookup"
    if tool_name == "TodoWrite":
        return "\U0001f4dd updating plan"
    return tool_name or "working"


def last_assistant_text(transcript_path):
    """Return the text of the most recent assistant message, or ''."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        with open(transcript_path, "r", errors="replace") as f:
            lines = f.readlines()[-400:]
    except Exception:
        return ""
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "assistant":
            continue
        msg = obj.get("message", obj)
        content = msg.get("content", "")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
        text = " ".join(t for t in texts if t).strip()
        if text:
            return text
    return ""


def looks_like_question(text):
    if not text:
        return False
    tail = text[-400:]
    if "?" in tail or "？" in tail:
        return True
    cues = ["还是", "请问", "你想", "要不要", "哪一个", "哪种", "应该用",
            "which", "should i", "do you want", "prefer", "or b?", "let me know",
            "confirm", "确认一下"]
    low = tail.lower()
    return any(c in tail or c in low for c in cues)


def snippet(text, n=140):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


def load_record(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def write_record(path, rec):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def notify(title, body, sound="Glass"):
    """Fire a macOS notification without blocking the caller."""
    def san(s):
        return re.sub(r"\s+", " ", (s or "")).replace("\\", " ").replace('"', "'").strip()[:200]

    title, body = san(title), san(body)
    script = f'display notification "{body}" with title "{title}" sound name "{sound}"'
    try:
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log_debug(f"notify failed: {e}")


def main():
    payload = read_payload()
    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd") or os.getcwd()
    log_debug(f"event={event} sid={session_id[:8]}")

    path = os.path.join(STATE_DIR, f"{session_id}.json")

    # SessionEnd: drop the terminal from the board.
    if event == "SessionEnd":
        try:
            os.remove(path)
        except OSError:
            pass
        return

    rec = load_record(path)
    now = time.time()
    prev_state = rec.get("state")

    rec["session_id"] = session_id
    rec["cwd"] = cwd
    rec["short_cwd"] = short_path(cwd)
    rec["project"] = project_name(cwd)
    # Per-terminal label override (set FLEET_LABEL before launching claude).
    label = os.environ.get("FLEET_LABEL")
    if label:
        rec["label"] = label.strip()
    rec["updated_at"] = now

    new_state = rec.get("state", IDLE)
    substate = rec.get("substate", "")
    do_notify = None  # (title, body, sound)

    if event == "SessionStart":
        new_state, substate = IDLE, "started"
        rec["last_action"] = "session started"

    elif event == "UserPromptSubmit":
        new_state, substate = WORKING, ""
        rec["last_action"] = "\U0001f195 new task received"

    elif event in ("PreToolUse", "PostToolUse"):
        new_state, substate = WORKING, ""
        rec["last_action"] = describe_tool(payload.get("tool_name", ""),
                                           payload.get("tool_input"))

    elif event == "Notification":
        # Permission request, or Claude idle-waiting for your input.
        new_state, substate = NEEDS_YOU, "waiting"
        msg = payload.get("message", "Claude is waiting for you")
        rec["last_action"] = "\U0001f7e1 " + snippet(msg, 60)
        rec["last_message"] = snippet(msg, 180)
        name = rec.get("label") or rec.get("project")
        recent = (now - rec.get("last_notify_ts", 0)) < RENOTIFY_COOLDOWN
        if not (prev_state in (NEEDS_YOU, AWAITING) and recent):
            do_notify = (f"\U0001f7e1 {t('hook.needs_you', name=name)}",
                         msg, "Funk")

    elif event == "Stop":
        text = last_assistant_text(payload.get("transcript_path"))
        name = rec.get("label") or rec.get("project")
        if looks_like_question(text):
            new_state, substate = AWAITING, "question"
            rec["last_action"] = "\U0001f4ac wants to discuss"
            rec["last_message"] = snippet(text, 180)
            do_notify = (f"\U0001f4ac {t('hook.discuss', name=name)}",
                         text or "Claude has a question", "Ping")
        else:
            new_state, substate = AWAITING, "done"
            rec["last_action"] = "✅ finished turn"
            rec["last_message"] = snippet(text, 180) if text else ""
            do_notify = (f"✅ {t('hook.done', name=name)}",
                         text or "Turn finished", "Glass")

    # Track when the current state began (for dwell time on the board).
    if new_state != prev_state:
        rec["state_since"] = now
    rec.setdefault("state_since", now)
    rec["state"] = new_state
    rec["substate"] = substate

    if do_notify:
        rec["last_notify_ts"] = now
        rec["last_notify_substate"] = substate

    write_record(path, rec)

    if do_notify:
        notify(*do_notify)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_debug(f"FATAL {e!r}")
    sys.exit(0)
