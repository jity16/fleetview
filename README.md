<div align="center">

# FleetView

### Never miss a Claude Code session that needs you.

Run `claude` in a dozen terminals? FleetView puts them all on **one board**, pins
the one waiting on you to the top, and fires a **native macOS notification** the
moment it needs you — then jumps you to its pane with **one key**.

[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.7%2B-3776AB.svg)](#-quick-start)
[![macOS notifications](https://img.shields.io/badge/macOS-notifications-000000.svg?logo=apple)](#-features)
[![deps](https://img.shields.io/badge/dependencies-zero-a6e3a1.svg)](#)
[![i18n](https://img.shields.io/badge/i18n-en%20%2F%20%E4%B8%AD%E6%96%87-f9e2af.svg)](#-configuration)

**English** · [简体中文](README.zh-CN.md)

<img src="assets/demo.svg" width="820" alt="A session asks a question, FleetView pins it to the top and pops a macOS notification, you press its hotkey and jump to the pane.">

</div>

You open `claude` by hand in terminal after terminal, and then you lose the
thread — *which one is waiting on me right now?* You end up alt-tabbing through a
wall of look-alike tabs to find the one that quietly asked you a question ten
minutes ago.

**FleetView is one always-on board for all of them.** The sessions that need you
float to the top, a macOS notification fires the moment one does, and a single
keypress drops you into the right pane. No `pip install`, no daemon, no config
files — it's pure Python standard library. Clone it, run `fleet`, done.

## ✨ Features

- 🔔 **Native macOS notifications.** The instant a session finishes a turn or
  asks you a question, you get a real macOS notification — even when Claude is in
  the background or you're off in another app. The project name leads the title,
  and a text question lands the **question itself** in the banner. Want it to
  linger on screen? `--sticky`.
- 🗂️ **Every session on one board.** All your hand-opened `claude` terminals,
  grouped by project, with the ones **waiting on you pinned to the top**. Idle
  terminals fold into a single line so they can't bury what matters.
- 🎯 **One-key jump.** Click a row, or press its letter, and you're teleported
  straight to that tmux pane — no more hunting through terminal tabs. **Zero
  setup:** no hook, no env var, nothing to install.
- 💬 **See what each one is doing.** Every row shows the session's title and its
  latest line. And a plain-text question Claude leaves you — the kind that
  otherwise just looks "idle" — gets **spotted and bumped to the top**.
- 📊 **Know how hard you're pushing.** A live, per-model usage gauge over the
  rolling 5-hour window, from real token counts across your whole fleet.
- 🌐 **English / 中文** (`--lang` or `FLEET_LANG`) · 🪶 **zero install, zero
  dependencies** — pure stdlib Python 3, nothing touched on your system.

## 🤔 Why not just `claude agents`?

The official `claude agents` **TUI only lists _background_ sessions** (the ones
you dispatch with `claude --bg` / `/bg`). The interactive sessions you open by
hand in each terminal — the ones you actually babysit — **don't show up there.**
FleetView shows exactly those.

## 🎬 See it

A fuller fleet: two sessions waiting on you (with the question they're asking),
idle terminals folded away, a live per-model usage line, and the recent-alerts feed.

<div align="center">
<img src="assets/board.svg" width="820" alt="FleetView board: 8 sessions grouped by project, NEEDS YOU rows pinned with their question, folded idle lines, a usage estimate, and an alerts feed.">
</div>

## 🚀 Quick start

```bash
git clone https://github.com/jity16/Fleet_ClaudeCode.git
cd Fleet_ClaudeCode

# add a `fleet` shortcut (writes the absolute path for you)
echo "alias fleet=\"python3 $(pwd)/fleet_board.py\"" >> ~/.zshrc && source ~/.zshrc

fleet
```

That's it — the board comes up and starts watching every session, notifications
on. (No alias? Just run `python3 fleet_board.py` or `./fleet`. On bash, use
`~/.bashrc`.)

> **Requires** Python 3.7+, Claude Code ≥ v2.1.139, and macOS (for notifications +
> the tab jump). `tmux` is optional, but needed to jump to panes.

## ⚙️ Configuration

Everything is a flag — there are no config files. Plain `fleet` is the recommended
setup; the rest is opt-in.

| Command | What it does |
| :------ | :----------- |
| `fleet` | Live board, refresh every 2s, **notifications on**. |
| `fleet --lang zh` | Chinese UI (see [Language](#language)). |
| `fleet jump` | Jump to the session waiting on you. |
| `fleet jump <pid\|target>` | Jump to a specific session (pid or `sess:win.pane`). |
| `fleet --headless &` | Notifier only — no board, just pings. `nohup`-safe. |
| `fleet --sticky` | Persistent alerts that linger until dismissed (~60s). |
| `fleet --all` | List every idle session individually (default folds them). |
| `fleet --once` | Render one frame and exit. |
| `fleet --no-notify` | Board only, no notifications. |
| `fleet --interval N` | Refresh every `N` seconds (default 2). |
| `fleet --usage` | One-shot detailed usage breakdown, then exit. |
| `fleet --budget 20M` | Use a fixed weighted-5h ceiling for the `now` gauge. |
| `fleet --no-usage` | Hide the usage line. |
| `fleet --test-notify` | Fire a sample notification (confirm macOS lets it through). |

### Make notifications land (and linger)

macOS banners auto-dismiss in a few seconds by OS policy. Two ways to keep them up:

- **`fleet --sticky`** — notifications become persistent alerts that stay until
  you dismiss them (or ~60s). No system change needed.
- **Alerts style** — *System Settings → Notifications → Script Editor* → set the
  style to **Alerts** (not Banners).

If nothing pops at all, run `fleet --test-notify`, then allow notifications for
**Script Editor** (osascript) under *System Settings → Notifications*.

### Language

The UI speaks **English** or **中文**, resolved in this order:

```bash
fleet --lang zh           # 1. explicit flag (this run)
export FLEET_LANG=zh      # 2. env var (every run)
# 3. your locale ($LANG / $LC_ALL starting with "zh" → Chinese)
# 4. English
```

### Bind `jump` to a tmux key

So a single chord pulls you to whoever's waiting, without even looking at the board:

```tmux
# ~/.tmux.conf  →  prefix + J jumps to the session waiting on you
bind-key J run-shell "python3 /path/to/fleet_board.py jump"
```

## ⚠️ Good to know

- **macOS-only for notifications and the tab jump.** The board itself runs
  anywhere Python does; the notifications use macOS, and `tmux` is needed to jump
  to panes.
- **The usage gauge is an _estimate_, not your official plan-limit %.** Anthropic's
  real `/usage` figure isn't exposed to scripts, so FleetView estimates it from
  local token counts (and never touches your credentials). Pass `--budget <N>`
  once you learn your true ceiling.

## Contributing

Issues and PRs welcome — it's a small, dependency-free codebase. The demo images
in this README are hand-rolled SVGs in [`assets/`](assets), so they stay in sync
with what the tool actually draws.

## License

[MIT](LICENSE) © jity16
