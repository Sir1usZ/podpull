# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [0.2.0] — 2026-06-29

UX overhaul. Adds dependencies: `rich`, `rich-argparse`, `questionary`.

### Added
- **Interactive multi-select picker**: `podget get <show>` with no selector opens a
  keyboard-driven checkbox list (↑/↓ move, space toggle, `a` select-all, enter confirm).
- **Progress feedback**: spinners with step messages for network fetches (resolving
  show → fetching feed → parsing), and `rich` progress bars (size, speed, ETA) for downloads.
- **Colored, example-rich help** via `rich-argparse`; grouped `get` selection options.
- `--no-input` flag on `get` to never prompt (for scripts/CI).

### Changed
- Saved file paths print to **stdout**; all UI (spinners, tables, bars, messages) to **stderr** — clean for scripting.
- `search`/`list`/`info` now render as tidy tables.
- Non-interactive / piped `get <show>` without a selector falls back to a listing + hint instead of hanging.
- `core.download_url` reports progress via an `on_progress(done, total)` callback (core stays dependency-free).

## [0.1.0] — 2026-06-29

Initial public release. Core feature set, Python standard library only.

### Added
- `podget search <term>` — find shows via the iTunes Search API.
- `podget info <src>` — show metadata (title, author, Apple id, feed, episode count, latest).
- `podget list <src>` — list episodes, with `--match REGEX`, `--all`, `--limit`.
- `podget get <src>` — download episode audio, by `--match` / `--latest N` / `--index 0,2`.
- Direct episode links: xiaoyuzhou (`og:audio`) and Apple (`?i=`, matched in feed; `yt-dlp` fallback).
- Resumable, stdlib-only downloader; filenames as `YYYY-MM-DD - title.ext`.
