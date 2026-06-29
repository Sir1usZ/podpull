"""podget command-line interface.

UI (colors, spinners, progress bars, interactive selection) lives here; all
network/parse logic lives in `core` and stays dependency-free. File paths are
printed to stdout (scriptable); everything else goes to stderr.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime

from rich.console import Console
from rich.progress import (BarColumn, DownloadColumn, Progress, SpinnerColumn,
                           TextColumn, TimeRemainingColumn, TransferSpeedColumn)
from rich.table import Table

from . import __version__, core

try:
    from rich_argparse import RawDescriptionRichHelpFormatter as _Formatter
except Exception:                      # pragma: no cover - fallback if absent
    _Formatter = argparse.RawDescriptionHelpFormatter

DEFAULT_OUT = os.path.expanduser("~/Downloads/Podcasts")
out_console = Console()                # stdout — machine-readable (file paths)
ui = Console(stderr=True)              # stderr — humans (spinners, tables, bars)


def _err(msg: str) -> None:
    ui.print(f"[bold red]podget:[/] {msg}")


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _resolve_show(kind: str, s: str) -> core.Show:
    """Resolve a show/feed with step-by-step spinner feedback."""
    with ui.status("[cyan]Resolving show via iTunes…", spinner="dots") as status:
        if kind == "apple_show":
            feed, name, author, pid = core.apple_show_to_feed(s)
        else:                          # rss
            feed, name, author, pid = s, "", "", ""
        status.update("[cyan]Fetching RSS feed & parsing episodes…")
        title, feed_author, eps = core.parse_feed(feed)
    return core.Show(title=name or title, feed=feed,
                     author=author or feed_author, apple_id=pid, episodes=eps)


def _download_all(episodes: list[core.Episode], out_dir: str) -> int:
    """Download episodes with a live per-file progress bar."""
    n = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.fields[label]}", justify="left"),
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=ui,
        transient=True,
    ) as progress:
        for ep in episodes:
            task = progress.add_task("dl", label=ep.title[:34] or "episode", total=None)

            def cb(done: int, total: int, _t=task) -> None:
                progress.update(_t, completed=done, total=(total or None))

            try:
                path = core.download_episode(ep, out_dir, on_progress=cb)
            except Exception as e:      # keep going on a single failure
                progress.console.print(f"[red]✗[/] {ep.title[:50]}: {e}")
                continue
            progress.remove_task(task)
            size = os.path.getsize(path)
            ui.print(f"[green]✓[/] {ep.date}  {ep.title[:50]}  [dim]({size/1e6:.1f} MB)[/]")
            print(path)                 # stdout: the saved file path
            n += 1
    return n


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_search(args) -> int:
    with ui.status(f"[cyan]Searching iTunes for “{args.term}”…"):
        results = core.search_shows(args.term, limit=args.limit, country=args.country)
    if not results:
        _err("no shows found")
        return 1
    table = Table(title=f"Podcasts matching “{args.term}”", header_style="bold", expand=False)
    table.add_column("Apple ID", style="cyan", no_wrap=True)
    table.add_column("Eps", justify="right", style="magenta")
    table.add_column("Show")
    table.add_column("Author", style="dim")
    for r in results:
        table.add_row(str(r.get("collectionId")), str(r.get("trackCount") or "?"),
                      r.get("collectionName") or "", r.get("artistName") or "")
    ui.print(table)
    ui.print("[dim]Next:[/] podget list <Apple ID>  •  podget get <Apple ID>")
    return 0


def cmd_info(args) -> int:
    kind, s = core.classify(args.src)
    if kind not in ("apple_show", "rss"):
        _err("info needs a show URL/id or RSS feed (not an episode link)")
        return 1
    show = _resolve_show(kind, s)
    latest = show.episodes[0] if show.episodes else None
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    table.add_row("Title", show.title)
    table.add_row("Author", show.author or "[dim]—[/]")
    if show.apple_id:
        table.add_row("Apple ID", show.apple_id)
    table.add_row("Feed", show.feed)
    table.add_row("Episodes", str(len(show.episodes)))
    if latest:
        table.add_row("Latest", f"{latest.date}  {latest.title}")
    ui.print(table)
    return 0


def cmd_list(args) -> int:
    kind, s = core.classify(args.src)
    if kind not in ("apple_show", "rss"):
        _err("list needs a show URL/id or RSS feed (not an episode link)")
        return 1
    show = _resolve_show(kind, s)
    eps = core.select(show.episodes, match=args.match) if args.match else show.episodes
    if not args.all and not args.match:
        eps = eps[: args.limit]
    if not eps:
        _err("no episodes matched")
        return 1
    table = Table(title=f"{show.title} — {len(show.episodes)} episodes", header_style="bold")
    table.add_column("#", justify="right", style="magenta", no_wrap=True)
    table.add_column("Date", style="cyan", no_wrap=True)
    table.add_column("Title")
    for i, e in enumerate(eps):
        table.add_row(str(i), e.date, e.title)
    ui.print(table)
    ui.print("[dim]Download:[/] podget get <src> --index N[,N]  •  --match RE  •  --latest N "
             "•  or just `podget get <src>` to pick interactively")
    return 0


def _ytdlp_fallback(src: str, out_dir: str) -> int:
    if not shutil.which("yt-dlp"):
        _err("episode not in recent list and yt-dlp not installed for fallback")
        return 2
    os.makedirs(out_dir, exist_ok=True)
    tmpl = os.path.join(out_dir, "%(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s")
    return subprocess.run(["yt-dlp", "--no-playlist", "-o", tmpl, src]).returncode


def _interactive_select(show: core.Show) -> list[core.Episode]:
    import questionary
    if not show.episodes:
        return []
    choices = [questionary.Choice(title=f"{e.date}  {e.title}", value=i)
               for i, e in enumerate(show.episodes)]
    picked = questionary.checkbox(
        f"Select episodes from “{show.title}”   "
        "(↑/↓ move · space toggle · a all · enter confirm)",
        choices=choices,
    ).ask()                            # None on Ctrl-C/Esc
    return [show.episodes[i] for i in (picked or [])]


def cmd_get(args) -> int:
    kind, s = core.classify(args.src)
    out = args.out

    # --- pasted episode links: download immediately ----------------------- #
    if kind == "xyz_episode":
        with ui.status("[cyan]Resolving xiaoyuzhou episode…"):
            url, title = core.xyz_episode_to_audio(s)
        return 0 if _download_all([core.Episode(title=title, pub="", url=url,
                                                mime="audio/mp4")], out) else 1

    if kind == "apple_episode":
        with ui.status("[cyan]Resolving Apple episode…"):
            url, title, rel = core.apple_episode_to_audio(s)
        if not url:
            _err("episode beyond recent catalog; trying yt-dlp")
            return _ytdlp_fallback(s, out)
        try:
            pub = datetime.fromisoformat((rel or "").replace("Z", "+00:00")).strftime(
                "%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            pub = ""
        return 0 if _download_all([core.Episode(title=title or "episode", pub=pub,
                                                url=url)], out) else 1

    # --- show / rss: select then download --------------------------------- #
    show = _resolve_show(kind, s)
    sel = core.select(show.episodes, match=args.match, latest=args.latest, index=args.index)

    if not sel and not (args.match or args.latest or args.index):
        # no selector given -> interactive picker, or list+hint if not a TTY
        if _interactive() and not args.no_input:
            sel = _interactive_select(show)
            if not sel:
                _err("nothing selected")
                return 1
        else:
            cmd_list(argparse.Namespace(src=s, match=None, all=False, limit=20))
            _err("no selector and not an interactive terminal — pass "
                 "--match RE / --latest N / --index 0,2")
            return 1

    if not sel:
        _err("no episode matched your selection")
        return 1

    ui.print(f"[bold]Downloading {len(sel)} episode(s)[/] from “{show.title}” → [dim]{out}[/]")
    return 0 if _download_all(sel, out) else 1


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
EXAMPLES = """
[bold]Examples[/]
  [cyan]podget search[/] "睡前故事"                 find shows (Apple ID, episode count)
  [cyan]podget info[/]  1532755821                  show metadata + latest episode
  [cyan]podget list[/]  1532755821 --match EP34     list episodes (filter by title regex)

  [cyan]podget get[/]   1532755821                  ← pick episodes interactively (↑/↓, space, enter)
  [cyan]podget get[/]   1532755821 --latest 3       newest 3 episodes
  [cyan]podget get[/]   1532755821 --index 0,2,5    by list number (0 = newest)
  [cyan]podget get[/]   1532755821 --match "牛頭人"  by title regex
  [cyan]podget get[/]   "https://www.xiaoyuzhoufm.com/episode/<id>"   a pasted link

[dim]<src> = Apple show URL · bare Apple ID · RSS feed URL · Apple ?i= episode URL · xiaoyuzhou link[/]
[dim]Downloads default to ~/Downloads/Podcasts (override with --out).[/]
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="podget", formatter_class=_Formatter,
        description="Download specific podcast episode audio from Apple Podcasts, "
                    "RSS feeds, or xiaoyuzhou links.",
        epilog=EXAMPLES)
    p.add_argument("--version", action="version", version=f"podget {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    s = sub.add_parser("search", help="search for podcast shows", formatter_class=_Formatter,
                       description="Search the iTunes catalog for shows by name/keyword.")
    s.add_argument("term", help="search keywords, e.g. \"睡前故事\"")
    s.add_argument("--limit", type=int, default=10, metavar="N", help="max results (default 10)")
    s.add_argument("--country", default="US", metavar="CC", help="iTunes storefront (default US)")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("info", help="show metadata for a podcast", formatter_class=_Formatter,
                       description="Print a show's title, author, feed, episode count, and latest episode.")
    s.add_argument("src", help="Apple show URL/id or RSS feed URL")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("list", help="list a show's episodes", formatter_class=_Formatter,
                       description="List episodes (newest first). # is the index used by `get --index`.")
    s.add_argument("src", help="Apple show URL/id or RSS feed URL")
    s.add_argument("--match", metavar="RE", help="case-insensitive title regex filter")
    s.add_argument("--all", action="store_true", help="show every episode (not just recent)")
    s.add_argument("--limit", type=int, default=40, metavar="N", help="how many recent to show (default 40)")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("get", help="download episode audio", formatter_class=_Formatter,
                       description="Download one or more episodes. With a show/feed and no selector, "
                                   "opens an interactive multi-select picker.",
                       epilog=EXAMPLES)
    s.add_argument("src", help="show URL/id, RSS URL, or a single-episode link")
    g = s.add_argument_group("episode selection (omit all → interactive picker)")
    g.add_argument("--match", metavar="RE", help="case-insensitive title regex")
    g.add_argument("--latest", type=int, metavar="N", help="newest N episodes")
    g.add_argument("--index", metavar="N[,N]", help="0-based list indices (0 = newest)")
    s.add_argument("--out", default=DEFAULT_OUT, metavar="DIR",
                   help=f"output directory (default: {DEFAULT_OUT})")
    s.add_argument("--no-input", action="store_true",
                   help="never prompt; fail instead of opening the interactive picker")
    s.set_defaults(func=cmd_get)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):     # bare `podget` -> show usage, not an error
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError) as e:
        _err(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
