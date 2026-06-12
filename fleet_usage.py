#!/usr/bin/env python3
"""
FleetView usage tracker — "how much did the whole fleet burn today?"

Computes today's and the last-5h token usage across ALL your Claude Code
sessions by reading the transcript JSONL files Claude Code writes under
    ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl
Each assistant turn there carries a real `message.usage` block (input / output /
cache-creation / cache-read tokens), a `model`, and a `timestamp`.

WHY transcripts: `claude agents --json` carries no token data, and there is no
command line for plan-quota usage — the `/usage` panel fetches that live from
Anthropic over OAuth and is neither scriptable nor cached on disk in a readable
form.  The transcripts are the only reliable LOCAL source of real token counts.

WHAT THIS IS NOT: it can't read your actual Pro/Max plan-limit percentage
(Anthropic doesn't publish the token→limit formula or expose the live gauge).
Treat these numbers as a faithful *proxy* for "how hard did I push the fleet" —
real token throughput — not as your official remaining quota.

Standalone:  python3 fleet_usage.py            # detailed breakdown, then exit
             python3 fleet_usage.py --window 5  # change the rolling window hours
"""

import json
import os
import time
from collections import deque

from fleet_i18n import t, set_lang_from_argv

HOME = os.path.expanduser("~")
PROJECTS_DIR = os.path.join(HOME, ".claude", "projects")

# Token buckets we track per scope. Keys kept short for compact dicts.
_BUCKET_KEYS = ("in", "out", "cw", "cr")  # input, output, cache-create, cache-read


def _empty_bucket():
    return {"in": 0, "out": 0, "cw": 0, "cr": 0}


def bucket_total(b):
    return b["in"] + b["out"] + b["cw"] + b["cr"]


# Cost-style weights, ~Anthropic's per-token price ratios (output is ~5x input,
# cache-write ~1.25x, cache-read ~0.1x).  Used for the *estimate* so the headline
# isn't drowned by cheap, plentiful cache reads — it tracks the expensive tokens
# that actually move you toward a rate limit.  Ratios are ~model-independent.
WEIGHTS = {"in": 1.0, "out": 5.0, "cw": 1.25, "cr": 0.1}


def bucket_weighted(b):
    return (b["in"] * WEIGHTS["in"] + b["out"] * WEIGHTS["out"] +
            b["cw"] * WEIGHTS["cw"] + b["cr"] * WEIGHTS["cr"])


def _add(dst, src):
    for k in _BUCKET_KEYS:
        dst[k] += src[k]


def _usage_to_bucket(u):
    return {
        "in": u.get("input_tokens", 0) or 0,
        "out": u.get("output_tokens", 0) or 0,
        "cw": u.get("cache_creation_input_tokens", 0) or 0,
        "cr": u.get("cache_read_input_tokens", 0) or 0,
    }


def fmt_tok(n):
    """Compact token count: 1234 → 1.2K, 18_400_000 → 18.4M."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    if n < 1_000_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n / 1_000_000_000:.2f}B"


def parse_budget(s):
    """'50M' / '5000000' / '5e6' → int tokens, or None on failure."""
    if not s:
        return None
    s = str(s).strip().upper().replace("_", "").replace(",", "")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


def model_family(model):
    m = (model or "").lower()
    for fam in ("opus", "sonnet", "haiku"):
        if fam in m:
            return fam
    return m.replace("claude-", "") or "?"


def _parse_ts(s):
    """ISO 8601 UTC like '2026-06-01T03:26:45.894Z' → epoch seconds, or None."""
    if not s or len(s) < 19:
        return None
    try:
        st = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        # struct_time is UTC here; timegm avoids the local-tz shift of mktime.
        return _timegm(st)
    except (ValueError, TypeError):
        return None


def _timegm(st):
    import calendar
    return calendar.timegm(st)


def _start_of_today_local(now=None):
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


# How fast a reference ceiling fades when you ease off, per local day. It still
# ratchets UP instantly when you push harder; decay only matters on quieter
# days, so a single monster session can't pin the gauge low forever. 0.95/day is
# roughly a two-week half-life.
PEAK_DECAY_PER_DAY = 0.95


def _today_str(now=None):
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _days_between(d1, d2):
    """Whole local days from date-string d1 to d2 ('YYYY-MM-DD'); 0 on error."""
    try:
        t1 = time.mktime(time.strptime(d1, "%Y-%m-%d"))
        t2 = time.mktime(time.strptime(d2, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return 0
    return int(round((t2 - t1) / 86400))


def _rolling_window_max(pairs, window):
    """Max sum of weights inside any `window`-second span. `pairs` is a list of
    (timestamp, weight) sorted ascending by timestamp."""
    dq = deque()
    running = 0.0
    peak = 0.0
    for ts, w in pairs:
        dq.append((ts, w))
        running += w
        while dq and dq[0][0] < ts - window:
            running -= dq.popleft()[1]
        if running > peak:
            peak = running
    return peak


def _project_for_cwd(cwd):
    """Same rule the board uses: nearest .git root's folder name, else basename."""
    cur = cwd or ""
    for _ in range(40):
        if cur and os.path.isdir(os.path.join(cur, ".git")):
            return os.path.basename(cur) or cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.basename((cwd or "").rstrip("/")) or (cwd or "?")


def _project_from_path(path):
    """Fallback when a transcript line has no cwd: decode the encoded dir name.
    Claude encodes the cwd by replacing '/' with '-', which is lossy, so we just
    take the last path-ish segment as a best effort."""
    enc = os.path.basename(os.path.dirname(path))
    seg = enc.rstrip("-").split("-")[-1]
    return seg or enc or "?"


class UsageTracker:
    """Incrementally tallies token usage across all transcripts.

    refresh() is throttled (min_interval) and reads only newly-appended bytes per
    file, so it stays cheap even on a 2s board loop and with large active logs.
    """

    def __init__(self, window_hours=5, min_interval=15.0):
        self.window = window_hours * 3600
        self.window_hours = window_hours
        self.min_interval = min_interval
        self._offsets = {}            # path -> bytes already consumed
        self._today_date = None       # local 'YYYY-MM-DD' the today-buckets cover
        self._today = _empty_bucket()
        self._by_project = {}         # project -> bucket (today)
        self._by_model = {}           # family -> bucket (today)
        self._events = deque()        # (epoch, bucket) within the rolling window
        self._cwd_cache = {}          # cwd -> project (memoised .git walk)
        self._last_refresh = 0.0
        self.last_error = None
        # Reference ceilings for the "usage now" estimate, per model family plus an
        # "_all" total: your own busiest weighted-5h window, ratcheted up live and
        # decayed on quiet days (see PEAK_DECAY_PER_DAY) so it adapts both ways
        # instead of only ever growing. Persisted; seeded once from recent
        # history. Real numbers from your data — no fabricated plan figures.
        self._peak_path = os.path.join(HOME, ".fleetview", "usage.json")
        self.peaks = self._load_peaks()
        if self.peaks is None:
            self.peaks = self._seed_peak()
        elif not any(k != "_all" for k in self.peaks):
            # legacy file carried only an overall ceiling — backfill the per-model
            # references from history so the gauge doesn't bootstrap to 100%.
            for k, v in self._seed_peak().items():
                self.peaks[k] = max(self.peaks.get(k, 0.0), v)
        self._save_peaks()

    @property
    def peak_weighted(self):
        """Back-compat: the overall (all-models) reference ceiling."""
        return self.peaks.get("_all", 0.0)

    # ── reference-peak persistence ────────────────────────────────────────────
    def _load_peaks(self):
        """Load persisted per-model ceilings, decayed for the days elapsed since
        they were saved (so a quiet stretch lowers the gauge). Falls back to the
        legacy single 'peak_weighted' field from older versions."""
        try:
            with open(self._peak_path) as f:
                d = json.load(f)
        except (OSError, ValueError, TypeError):
            return None
        peaks = d.get("peaks")
        if not isinstance(peaks, dict):
            legacy = d.get("peak_weighted")
            peaks = {"_all": legacy} if legacy is not None else None
        if not peaks:
            return None
        try:
            peaks = {k: float(v) for k, v in peaks.items() if v is not None}
        except (ValueError, TypeError):
            return None
        date = d.get("peak_date")
        if date:
            elapsed = _days_between(date, _today_str())
            if elapsed > 0:
                factor = PEAK_DECAY_PER_DAY ** min(elapsed, 60)
                peaks = {k: v * factor for k, v in peaks.items()}
        return peaks

    def _save_peaks(self):
        try:
            os.makedirs(os.path.dirname(self._peak_path), exist_ok=True)
            tmp = self._peak_path + f".tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"peaks": self.peaks, "peak_date": _today_str()}, f)
            os.replace(tmp, self._peak_path)
        except OSError:
            pass

    def _seed_peak(self, seed_days=30):
        """One-time scan of recent transcripts → busiest weighted rolling-5h
        window, per model family AND overall, so the gauge has a meaningful
        denominator from day one instead of bootstrapping to 100%. Bounded to
        files touched in the last `seed_days` days to stay quick. Returns a
        {fam: peak, '_all': peak} dict (empty if there's no history)."""
        now = time.time()
        floor = now - seed_days * 86400
        events = []
        try:
            proj_dirs = list(os.scandir(PROJECTS_DIR))
        except OSError:
            return {}
        for d in proj_dirs:
            if not d.is_dir():
                continue
            try:
                files = list(os.scandir(d.path))
            except OSError:
                continue
            for f in files:
                if not f.name.endswith(".jsonl"):
                    continue
                try:
                    if f.stat().st_mtime < floor:
                        continue
                except OSError:
                    continue
                self._collect_events(f.path, floor, events)
        if not events:
            return {}
        events.sort(key=lambda x: x[0])
        peaks = {"_all": _rolling_window_max(
            [(ts, w) for ts, w, _ in events], self.window)}
        for fam in {f for _, _, f in events}:
            peaks[fam] = _rolling_window_max(
                [(ts, w) for ts, w, f in events if f == fam], self.window)
        return {k: v for k, v in peaks.items() if v > 0}

    def _collect_events(self, path, floor, out):
        try:
            fh = open(path, "r", errors="replace")
        except OSError:
            return
        with fh:
            for line in fh:
                if '"assistant"' not in line or "usage" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                u = (obj.get("message", {}) or {}).get("usage")
                if not isinstance(u, dict):
                    continue
                ts = _parse_ts(obj.get("timestamp"))
                if ts is None or ts < floor:
                    continue
                fam = model_family((obj.get("message", {}) or {}).get("model"))
                out.append((ts, bucket_weighted(_usage_to_bucket(u)), fam))

    # ── scanning ────────────────────────────────────────────────────────────
    def refresh(self, force=False):
        now = time.time()
        if not force and (now - self._last_refresh) < self.min_interval:
            return
        self._last_refresh = now

        today_str = _today_str(now)
        if today_str != self._today_date:
            # New local day: drop the day accumulators (offsets stay; only newly
            # appended lines — which carry today's timestamps — will be counted).
            if self._today_date is not None:
                # a day boundary passed while running → fade the references once
                self.peaks = {k: v * PEAK_DECAY_PER_DAY
                              for k, v in self.peaks.items()}
                self._save_peaks()
            self._today_date = today_str
            self._today = _empty_bucket()
            self._by_project = {}
            self._by_model = {}

        start_today = _start_of_today_local(now)
        cutoff = min(start_today, now - self.window)

        try:
            entries = os.scandir(PROJECTS_DIR)
        except OSError as e:
            self.last_error = str(e)
            return

        for proj_dir in entries:
            if not proj_dir.is_dir():
                continue
            try:
                files = os.scandir(proj_dir.path)
            except OSError:
                continue
            for f in files:
                if not f.name.endswith(".jsonl"):
                    continue
                try:
                    stat = f.stat()
                except OSError:
                    continue
                offset = self._offsets.get(f.path, 0)
                # Already-seen file with no growth and last touched before our
                # window of interest → nothing new to read.
                if stat.st_size <= offset and stat.st_mtime < cutoff:
                    continue
                self._scan_file(f.path, offset, stat.st_size,
                                 start_today, now)

        # Expire events that have aged out of the rolling window.
        wstart = now - self.window
        while self._events and self._events[0][0] < wstart:
            self._events.popleft()

        # Ratchet each model's reference (and the overall) up to the busiest
        # weighted-5h we've now seen; quiet-day decay handles easing off.
        changed = False
        by_model = self.window_by_model_weighted()
        for key, val in list(by_model.items()) + [("_all", sum(by_model.values()))]:
            if val > self.peaks.get(key, 0.0):
                self.peaks[key] = val
                changed = True
        if changed:
            self._save_peaks()

    def _scan_file(self, path, offset, size, start_today, now):
        if size < offset:  # file truncated/rotated → re-read from the top
            offset = 0
        try:
            with open(path, "r", errors="replace") as fh:
                if offset:
                    fh.seek(offset)
                for line in fh:
                    self._ingest(line, path, start_today, now)
                self._offsets[path] = fh.tell()
        except OSError as e:
            self.last_error = str(e)

    def _ingest(self, line, path, start_today, now):
        line = line.strip()
        if not line or '"assistant"' not in line or "usage" not in line:
            return
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if obj.get("type") != "assistant":
            return
        msg = obj.get("message", {}) or {}
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            return
        bucket = _usage_to_bucket(usage)
        if not bucket_total(bucket):
            return

        ts = _parse_ts(obj.get("timestamp")) or now
        cwd = obj.get("cwd")
        if cwd:
            proj = self._cwd_cache.get(cwd)
            if proj is None:
                proj = self._cwd_cache[cwd] = _project_for_cwd(cwd)
        else:
            proj = _project_from_path(path)
        fam = model_family(msg.get("model"))

        if ts >= start_today:
            _add(self._today, bucket)
            _add(self._by_project.setdefault(proj, _empty_bucket()), bucket)
            _add(self._by_model.setdefault(fam, _empty_bucket()), bucket)
        if ts >= now - self.window:
            self._events.append((ts, bucket, fam))

    # ── readouts ──────────────────────────────────────────────────────────────
    def window_bucket(self):
        agg = _empty_bucket()
        for _, b, _fam in self._events:
            _add(agg, b)
        return agg

    def window_by_model_weighted(self):
        """Weighted token load in the rolling window, split by model family."""
        out = {}
        for _, b, fam in self._events:
            out[fam] = out.get(fam, 0.0) + bucket_weighted(b)
        return out

    def window_resets_in(self, now=None):
        """Seconds until the oldest event ages out of the rolling window — a
        rough 'when does the window start freeing up' estimate (None if empty)."""
        if not self._events:
            return None
        now = now if now is not None else time.time()
        return max(0.0, self._events[0][0] + self.window - now)

    def snapshot(self):
        """Plain-data view for the renderer."""
        wb = self.window_bucket()
        return {
            "today": dict(self._today),
            "today_total": bucket_total(self._today),
            "today_weighted": bucket_weighted(self._today),
            "window": wb,
            "window_total": bucket_total(wb),
            "window_weighted": bucket_weighted(wb),
            "window_by_model": self.window_by_model_weighted(),
            "window_hours": self.window_hours,
            "peaks": dict(self.peaks),
            "peak_weighted": self.peak_weighted,
            "window_resets_in": self.window_resets_in(),
            "by_project": {p: bucket_total(b) for p, b in self._by_project.items()},
            "by_model": {m: bucket_total(b) for m, b in self._by_model.items()},
        }


def text_bar(frac, width=14):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def fmt_dur(seconds):
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def estimate(snap, budget=None):
    """Turn a snapshot into an estimated 'how much consumed now' reading.

    Without a budget, each model family's weighted load in the rolling window is
    measured against ITS OWN reference (your recent busiest 5h for that model,
    which decays if you ease off) — because Pro/Max limits are per-model. The
    headline is the model closest to its own ceiling (the binding constraint),
    with `per_model` carrying the full split. A --budget collapses this to a
    single combined figure against that budget.

    Returns None if there's no reference yet (brand-new install, no history)."""
    overall = snap["window_weighted"]
    if budget:
        return {
            "pct": overall / budget,
            "current": overall,
            "reference": budget,
            "source": "budget",
            "per_model": [],
            "headline_model": None,
            "resets_in": snap.get("window_resets_in"),
        }

    wm = snap.get("window_by_model") or {}
    peaks = snap.get("peaks") or {}
    ref_all = peaks.get("_all") or 0
    per = []
    for fam, cur in sorted(wm.items(), key=lambda x: -x[1]):
        ref = peaks.get(fam) or ref_all  # fall back to overall until a fam peak forms
        if ref:
            per.append({"model": fam, "pct": cur / ref,
                        "current": cur, "reference": ref})
    if not per and not ref_all:
        return None
    if per:
        head = max(per, key=lambda x: x["pct"])
        head_pct, head_model = head["pct"], head["model"]
    else:
        head_pct, head_model = (overall / ref_all if ref_all else 0), None
    return {
        "pct": head_pct,
        "current": overall,
        "reference": ref_all,
        "source": "peak",
        "per_model": per,
        "headline_model": head_model,
        "resets_in": snap.get("window_resets_in"),
    }


# ── standalone detailed dump ──────────────────────────────────────────────────
def dump(hours=5, budget=None):
    """Print a full one-shot usage breakdown to stdout."""
    tracker = UsageTracker(window_hours=hours)
    tracker.refresh(force=True)
    snap = tracker.snapshot()

    td = snap["today"]
    est = estimate(snap, budget)
    now_label = t("dump.now")
    print()
    print(f"  {t('dump.title')}")
    print("  " + "─" * 68)

    if est:
        if est["source"] == "budget":
            print(f"  {now_label}  [{text_bar(est['pct'])}] ~{est['pct'] * 100:.0f}%   "
                  f"(est. of your set budget)")
            print(f"            {fmt_tok(est['current'])} weighted now  /  "
                  f"{fmt_tok(est['reference'])} ref   ·   window frees up in "
                  f"~{fmt_dur(est['resets_in'])}")
        else:
            head = est.get("headline_model")
            htag = f" — {head} is closest to its limit" if head else ""
            print(f"  {now_label}  [{text_bar(est['pct'])}] "
                  f"~{est['pct'] * 100:.0f}%{htag}")
            print(f"            window frees up in ~{fmt_dur(est['resets_in'])}"
                  f"   ·   ref = your recent busiest 5h per model "
                  f"(decays if you ease off)")
            for p in est.get("per_model", []):
                print(f"              {p['model']:<7} [{text_bar(p['pct'], 10)}] "
                      f"~{p['pct'] * 100:>3.0f}%   "
                      f"{fmt_tok(p['current'])} / {fmt_tok(p['reference'])}")
        print()

    print(f"  TODAY    {fmt_tok(snap['today_total']):>8} raw  "
          f"({fmt_tok(snap['today_weighted'])} weighted)")
    print(f"           out {fmt_tok(td['out'])} · in {fmt_tok(td['in'])} · "
          f"cache-wr {fmt_tok(td['cw'])} · cache-rd {fmt_tok(td['cr'])}")
    print(f"  LAST {int(hours)}h  {fmt_tok(snap['window_total']):>8} raw  "
          f"({fmt_tok(snap['window_weighted'])} weighted — the 'now' window)")

    if snap["by_model"]:
        print("\n  by model (today):")
        for m, n in sorted(snap["by_model"].items(), key=lambda x: -x[1]):
            print(f"     {m:<8} {fmt_tok(n):>8}")

    if snap["by_project"]:
        print("\n  by project (today):")
        for p, n in sorted(snap["by_project"].items(), key=lambda x: -x[1]):
            print(f"     {fmt_tok(n):>8}  {p}")

    if not snap["today_total"]:
        print("\n  (no token usage recorded yet today)")
    print(f"\n  {t('dump.note')}")
    print()


def _main():
    import sys
    argv = sys.argv[1:]
    set_lang_from_argv(argv)
    hours = 5
    if "--window" in argv:
        try:
            hours = float(argv[argv.index("--window") + 1])
        except (ValueError, IndexError):
            pass
    budget = None
    if "--budget" in argv:
        try:
            budget = parse_budget(argv[argv.index("--budget") + 1])
        except IndexError:
            pass
    dump(hours=hours, budget=budget)


if __name__ == "__main__":
    _main()
