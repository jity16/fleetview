#!/usr/bin/env python3
"""
FleetView i18n — a tiny, dependency-free message catalog (English / 中文).

Pick the language, in order of precedence:
  1. an explicit `--lang en|zh` flag (parsed by the entry points), or set_lang()
  2. the FLEET_LANG environment variable ("en" / "zh" / "zh_CN" …)
  3. your locale (LC_ALL / LANG starting with "zh" → Chinese)
  4. English

Usage:
    from fleet_i18n import t, set_lang
    t("head.sessions", n=15)        # "15 sessions"  /  "15 个会话"

Only the *visible UI* is translated (the live board, notifications, the usage
line, --test-notify, the hook alerts). Operator-facing diagnostics stay English.
"""

import os

_LANG = None  # resolved lazily on first t()/lang() call


def _detect():
    env = (os.environ.get("FLEET_LANG") or "").lower()
    if env.startswith("zh"):
        return "zh"
    if env.startswith("en"):
        return "en"
    loc = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    if loc.startswith("zh"):
        return "zh"
    return "en"


def set_lang(lang):
    """Force the language ('en' or 'zh'); anything else is ignored."""
    global _LANG
    if lang in ("en", "zh"):
        _LANG = lang


def set_lang_from_argv(argv):
    """Honor a `--lang en|zh` flag if present; return the (possibly trimmed) argv
    unchanged (the flag is harmless to leave in place)."""
    if "--lang" in argv:
        try:
            set_lang(argv[argv.index("--lang") + 1])
        except IndexError:
            pass
    return argv


def lang():
    global _LANG
    if _LANG is None:
        _LANG = _detect()
    return _LANG


# key -> {"en": ..., "zh": ...}.  Values may contain {named} format fields.
MESSAGES = {
    # ── status labels (per row) ──
    "status.waiting": {"en": "NEEDS YOU", "zh": "需要你"},
    "status.busy":    {"en": "working",   "zh": "进行中"},
    "status.idle":    {"en": "idle",      "zh": "空闲"},

    # ── header ──
    "head.sessions": {"en": "{n} sessions", "zh": "{n} 个会话"},
    "head.waiting":  {"en": "needs you",    "zh": "在等你"},
    "head.busy":     {"en": "working",      "zh": "进行中"},
    "head.idle":     {"en": "idle",         "zh": "空闲"},
    "head.stale":    {"en": "stale {n}s",   "zh": "已陈旧 {n}s"},
    "hint.jump":     {"en": "click / key to jump · q quit",
                      "zh": "点击/按键跳转 · q 退出"},
    "hint.quit":     {"en": "Ctrl-C quit", "zh": "Ctrl-C 退出"},

    # ── usage line ──
    "usage.now":    {"en": "now",          "zh": "现在"},
    "usage.window": {"en": "window {d}",   "zh": "窗口 {d}"},
    "usage.today":  {"en": "today",        "zh": "今天"},

    # ── notifications ──
    "notify.waiting": {"en": "needs you", "zh": "在等你"},
    "notify.done":    {"en": "done",      "zh": "这轮做完了"},

    # ── alert log (the in-board 'recent alerts' feed) ──
    "event.waiting": {"en": "needs you", "zh": "在等你"},
    "event.done":    {"en": "done",      "zh": "完成"},

    # ── board chrome ──
    "board.connecting":  {"en": "connecting to `claude agents --json`…",
                          "zh": "正在连接 `claude agents --json`…"},
    "board.unreachable": {"en": "could not reach `claude agents --json`",
                          "zh": "无法连接 `claude agents --json`"},
    "board.retrying":    {"en": "retrying…  {clock}",
                          "zh": "重试中…  {clock}"},
    "board.none":        {"en": "No live Claude Code sessions.",
                          "zh": "没有正在运行的 Claude Code 会话。"},
    "board.alerts":      {"en": "recent alerts:", "zh": "最近提醒:"},
    "board.more":        {"en": " … +{n} more (resize / --all to expand)",
                          "zh": " … 还有 +{n} 条(放大窗口 / --all 展开)"},
    "board.today":       {"en": "today", "zh": "今天"},
    "board.idle_fold":   {"en": "{n} idle", "zh": "{n} 个空闲"},

    # ── --test-notify ──
    "test.title": {"en": "💬 FleetView test", "zh": "💬 FleetView 测试"},
    "test.body":  {"en": "If you can see this, notifications work ✅",
                   "zh": "如果你看到这条,通知就通了 ✅"},
    "test.sent":  {"en": "Test notification sent — check macOS Notification "
                         "Center (top-right).",
                   "zh": "已发送测试通知,检查 macOS 通知中心(右上角)。"},
    "test.allow": {"en": "If nothing appears: System Settings → Notifications → "
                         "find Script Editor / osascript and allow notifications.",
                   "zh": "若没弹出:系统设置 → 通知 → 找到 Script Editor / "
                         "osascript,允许通知。"},
    "test.linger": {"en": "Make alerts linger: add --sticky (a persistent "
                          "dialog), or set that app's style to “Alerts”.",
                    "zh": "想让通知停留更久:加 --sticky(持久弹窗),或把该 "
                          "app 的通知样式改成「提醒/Alerts」。"},

    # ── usage dump (fleet_usage.py --usage) ──
    "dump.title": {"en": "FleetView · usage estimate (real tokens from session "
                         "transcripts)",
                   "zh": "FleetView · 用量估算(来自会话 transcript 的真实 token)"},
    "dump.now":   {"en": "usage now", "zh": "现在消耗"},
    "dump.note":  {"en": "note: 'usage now' is an ESTIMATE — weighted tokens in "
                         "the rolling 5h window vs your own recent busiest 5h per "
                         "model (it decays if you ease off, or vs --budget). It is "
                         "NOT Anthropic's real plan-limit % (that lives only in "
                         "`/usage`, not exposed to scripts). Pass --budget <N> to "
                         "calibrate once you learn your true ceiling.",
                   "zh": "说明:「现在消耗」是估算 —— 滚动 5 小时窗口内的加权 token "
                         "对比你自己近期最忙的 5 小时(每个模型分别计算,长期不用会"
                         "衰减,或对比 --budget)。它不是 Anthropic 真实的套餐额度 % "
                         "(那个只在 `/usage` 里,脚本无法读取)。摸清真实上限后可用 "
                         "--budget <N> 校准。"},

    # ── one-shot snapshot (fleet_snapshot.py) ──
    "snapshot.note": {"en": "note: --json only exposes busy/idle; the real agent "
                            "view also shows each session's last reply and whether "
                            "it's waiting on you.",
                      "zh": "说明:--json 只给 busy/idle;真正的 agent view "
                            "还会显示「最后一条回复 / 是否在等你」。"},

    # ── hook notifications (fleet_hook.py) ──
    "hook.needs_you": {"en": "{name} needs you",      "zh": "{name} 需要你"},
    "hook.discuss":   {"en": "{name} wants to discuss", "zh": "{name} 想跟你讨论"},
    "hook.done":      {"en": "{name} done",            "zh": "{name} 完成"},
}


def t(key, **kw):
    """Look up `key` for the active language and format it with kwargs.
    Falls back to English, then to the raw key, so a missing entry never raises."""
    entry = MESSAGES.get(key)
    if entry is None:
        return key.format(**kw) if kw else key
    s = entry.get(lang()) or entry.get("en") or key
    try:
        return s.format(**kw) if kw else s
    except (KeyError, IndexError, ValueError):
        return s
