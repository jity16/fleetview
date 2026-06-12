#!/usr/bin/env python3
"""
FleetView session details — answer "what is each Claude session actually doing?"
with no hook, by reading the per-session transcript JSONL Claude Code already
writes at ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl.

From a transcript we pull:
  - topic:        the first real user prompt (what you set this session going on)
  - last:         the session's latest 'conclusion' line (what it just said /
                  the question it's asking)
  - is_question:  whether that line *ends* like a question — deliberately tight,
                  so a report/table that merely contains a '?' doesn't cry wolf.
                  Lets an `idle` session that actually asked you something be
                  re-surfaced as "needs you".

Cached per session and re-read only when the file's mtime changes, so it stays
cheap on the board's poll loop.
"""
import glob
import json
import os
import re

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")

_CLOSERS = "`*_)\"'）】」』.。 \t"


def _loads(line):
    if '"user"' not in line and '"assistant"' not in line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _msg_text(obj):
    msg = obj.get("message", obj)
    c = msg.get("content", "")
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _head(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _tail(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return ("…" + s[-(n - 1):]) if len(s) > n else s


def _conclusion(text):
    """The last 'prose' line of an assistant message — skipping table/list/code
    lines — which is usually its conclusion or the question it ends on."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    for ln in reversed(lines):
        if not ln:
            continue
        if ln[0] in "|-*>#`" or re.match(r"^\d+[.)]", ln):
            continue
        return ln
    for ln in reversed(lines):       # fallback: any non-empty line
        if ln:
            return ln
    return ""


def looks_like_question(line):
    """True when `line` ends like a question, after trimming trailing markdown /
    punctuation noise. Tight by design (end-anchored, not 'contains a ?')."""
    if not line:
        return False
    c = line.rstrip(_CLOSERS)
    return c.endswith("?") or c.endswith("？")


class Details:
    def __init__(self, topic_len=60, last_len=88):
        self.topic_len = topic_len
        self.last_len = last_len
        self._path = {}    # sid -> transcript path
        self._cache = {}   # sid -> {mtime, topic, last, is_question}

    def path_for(self, sid, cwd=None):
        p = self._path.get(sid)
        if p and os.path.isfile(p):
            return p
        if cwd:
            # Claude encodes the cwd by replacing '/' with '-'; try that directly
            # before falling back to a glob across all project dirs.
            guess = os.path.join(PROJECTS_DIR, cwd.replace("/", "-"),
                                 sid + ".jsonl")
            if os.path.isfile(guess):
                self._path[sid] = guess
                return guess
        hits = glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl"))
        if hits:
            self._path[sid] = hits[0]
            return hits[0]
        return None

    def get(self, sid, cwd=None):
        if not sid:
            return None
        path = self.path_for(sid, cwd)
        if not path:
            return None
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._cache.get(sid)
        if cached and cached["mtime"] == mtime:
            return cached
        keep = cached["topic"] if cached else None
        topic, line, title = self._extract(path, keep_topic=keep)
        title = _head(title, self.topic_len) if title else None
        rec = {"mtime": mtime, "title": title, "topic": topic,
               # `label` is the one-glance identity to show: Claude's own session
               # title if it has one, else the first prompt you set it going on.
               "label": title or topic,
               "last": _tail(line, self.last_len) if line else None,
               "is_question": looks_like_question(line)}
        self._cache[sid] = rec
        return rec

    def _extract(self, path, keep_topic=None, tail_bytes=200_000):
        """Bounded read: the topic only needs the file's head (and is cached
        after the first read); the latest conclusion and Claude's `ai-title` only
        need the tail. Stays cheap even on a multi-megabyte transcript."""
        topic = keep_topic
        if topic is None:                 # first real user prompt (head, once)
            try:
                with open(path, errors="replace") as f:
                    for _ in range(400):
                        line = f.readline()
                        if not line:
                            break
                        o = _loads(line)
                        if not o or o.get("type") != "user":
                            continue
                        txt = _msg_text(o)
                        if txt and txt[0] not in "<" and not txt.startswith("/"):
                            topic = _head(txt, self.topic_len)
                            break
            except OSError:
                return topic, "", None
        conclusion, title = "", None      # latest conclusion + ai-title (tail)
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    f.readline()          # drop the partial first line
                data = f.read().decode("utf-8", "replace")
            for line in reversed(data.splitlines()):  # newest first
                if title is None and '"ai-title"' in line:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        o = None
                    if o and o.get("type") == "ai-title" and o.get("aiTitle"):
                        title = o["aiTitle"].strip()
                elif not conclusion:
                    o = _loads(line)
                    if o and o.get("type") == "assistant":
                        txt = _msg_text(o)
                        if txt:
                            conclusion = _conclusion(txt)
                if title is not None and conclusion:
                    break
        except OSError:
            pass
        return topic, conclusion, title

    def annotate(self, sessions):
        """Attach a 'detail' dict (or None) to each session dict, in place."""
        for s in sessions:
            s["detail"] = self.get(s.get("sessionId"), s.get("cwd"))
        return sessions
