"""Core logic: resolve a source -> feed/episodes, search, select, download.

No third-party dependencies — only the Python standard library.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import html.entities
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

# A plain browser User-Agent. Some podcast CDNs (e.g. xiaoyuzhou's feed.xyzfm.space)
# return 403 to identifiable bot/tool UAs, so we present as a normal browser.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
ITUNES_SEARCH = "https://itunes.apple.com/search"


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass
class Episode:
    title: str
    pub: str            # raw RFC-822 pubDate string from the feed
    url: str            # direct enclosure audio URL
    mime: str = ""
    guid: str = ""
    link: str = ""

    @property
    def date(self) -> str:
        for parse in (
            lambda s: parsedate_to_datetime(s),                                   # RFC-822
            lambda s: datetime.fromisoformat(                                     # ISO-8601
                re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s.strip().replace("Z", "+00:00"))),
        ):
            try:
                return parse(self.pub).strftime("%Y-%m-%d")
            except Exception:
                continue
        return "0000-00-00"


@dataclass
class Show:
    title: str
    feed: str
    author: str = ""
    apple_id: str = ""
    episodes: list[Episode] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# http helpers
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_json(url: str):
    return json.load(fetch(url))


# --------------------------------------------------------------------------- #
# input classification
# --------------------------------------------------------------------------- #
def classify(src: str) -> tuple[str, str]:
    """Return (kind, normalized_src). kind ∈ {apple_show, apple_episode,
    xyz_episode, rss}. Ximalaya album links normalize to rss with .xml extension."""
    s = src.strip()
    if re.fullmatch(r"\d+", s):
        return "apple_show", s
    if "xiaoyuzhoufm.com/episode/" in s:
        return "xyz_episode", s
    if "podcasts.apple.com" in s and re.search(r"[?&]i=\d+", s):
        return "apple_episode", s
    if "podcasts.apple.com" in s:
        return "apple_show", s
    m = re.search(r"ximalaya\.com/album/(\d+)", s)
    if m:  # Ximalaya Podcast托管 albums expose RSS at album/<id>.xml
        return "rss", f"https://www.ximalaya.com/album/{m.group(1)}.xml"
    if s.startswith("http"):
        return "rss", s
    raise ValueError(f"Cannot classify input: {src!r}")


# --------------------------------------------------------------------------- #
# search / resolve
# --------------------------------------------------------------------------- #
def search_shows(term: str, limit: int = 10, country: str = "US") -> list[dict]:
    q = urllib.parse.urlencode(
        {"term": term, "media": "podcast", "entity": "podcast",
         "limit": limit, "country": country}
    )
    return fetch_json(f"{ITUNES_SEARCH}?{q}").get("results", [])


def apple_show_to_feed(src: str) -> tuple[str, str, str, str]:
    """-> (feedUrl, name, author, apple_id)."""
    m = re.search(r"/id(\d+)", src) or re.fullmatch(r"(\d+)", src)
    if not m:
        raise ValueError(f"No Apple podcast id (idNNN) found in: {src}")
    pid = m.group(1)
    results = fetch_json(f"{ITUNES_LOOKUP}?id={pid}").get("results", [])
    r = results[0] if results else {}
    feed = r.get("feedUrl")
    if not feed and pi_credentials():      # second directory, BYOK-only
        feed = pi_feed_by_itunes_id(pid)
    if not feed:
        if not results:
            raise ValueError(f"iTunes lookup returned nothing for id={pid}")
        raise ValueError(f"No feedUrl for id={pid} (not a podcast?)")
    return feed, r.get("collectionName", ""), r.get("artistName", ""), pid


# --------------------------------------------------------------------------- #
# Podcast Index (optional, BYOK) — free keys at https://api.podcastindex.org
# Active only when both env vars are set; otherwise podpull never contacts PI.
# --------------------------------------------------------------------------- #
PODCASTINDEX_API = "https://api.podcastindex.org/api/1.0"


def pi_credentials() -> "tuple[str, str] | None":
    key = os.environ.get("PODCASTINDEX_API_KEY", "").strip()
    secret = os.environ.get("PODCASTINDEX_API_SECRET", "").strip()
    return (key, secret) if key and secret else None


def _pi_headers(key: str, secret: str, now: "int | None" = None) -> dict:
    ts = str(int(time.time()) if now is None else now)
    auth = hashlib.sha1((key + secret + ts).encode()).hexdigest()
    return {"User-Agent": UA, "X-Auth-Key": key, "X-Auth-Date": ts,
            "Authorization": auth}


def _pi_get(path: str, params: dict) -> dict:
    creds = pi_credentials()
    if not creds:
        raise ValueError("Podcast Index credentials not set "
                         "(PODCASTINDEX_API_KEY / PODCASTINDEX_API_SECRET)")
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{PODCASTINDEX_API}{path}?{q}",
                                 headers=_pi_headers(*creds))
    return json.load(urllib.request.urlopen(req, timeout=45))


def pi_search_shows(term: str, limit: int = 10) -> list:
    """Search Podcast Index; rows use the same keys as iTunes search results
    so the CLI table code needs no changes."""
    data = _pi_get("/search/byterm", {"q": term, "max": limit})
    return [{"collectionId": f.get("itunesId") or "",
             "collectionName": f.get("title") or "",
             "artistName": f.get("author") or "",
             "feedUrl": f.get("url") or "",
             "trackCount": f.get("episodeCount") or 0}
            for f in data.get("feeds", [])]


def pi_feed_by_itunes_id(pid: str) -> "str | None":
    """Second-directory feed lookup by Apple ID. Never raises — returns None
    so callers degrade gracefully when PI is down or has no entry."""
    try:
        feed = _pi_get("/podcasts/byitunesid", {"id": pid}).get("feed") or {}
        return (feed.get("url") or None) if isinstance(feed, dict) else None
    except Exception:
        return None


def _localname(tag) -> str:
    """'{ns}Tag' -> 'tag'. Comments/PIs have non-str tags -> ''."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _clean(text) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _first_text(el, *names) -> str:
    """First non-empty direct-child text whose localname is in names.
    Bare (un-namespaced) tags win over namespaced synonyms, so a real
    <title> beats <itunes:title> regardless of document order."""
    wanted = {n.lower() for n in names}
    candidates = [c for c in el if _localname(c.tag) in wanted]
    for c in candidates:
        if isinstance(c.tag, str) and "}" not in c.tag and _clean(c.text):
            return _clean(c.text)
    for c in candidates:
        if _clean(c.text):
            return _clean(c.text)
    return ""


def _author(el) -> str:
    """itunes:author / atom author>name -> managingEditor -> dc:creator."""
    for name in ("author", "managingeditor", "creator"):
        for child in el:
            if _localname(child.tag) == name:
                txt = _first_text(child, "name") or _clean(child.text)
                if txt:
                    return txt
    return ""


def _pub(item) -> str:
    """pubDate -> dc:date -> atom published -> atom updated (raw string)."""
    for name in ("pubdate", "date", "published", "updated"):
        for child in item:
            if _localname(child.tag) == name and _clean(child.text):
                return child.text.strip()
    return ""


def _find_enclosure(item) -> tuple[str, str]:
    """-> (audio_url, mime) or ('', ''). Priority: enclosure > media:content
    (audio) > atom link rel=enclosure. First match wins — WavPub/Omny items
    carry BOTH enclosure and media:content; one item must yield one URL."""
    for child in item:
        if _localname(child.tag) == "enclosure" and child.get("url"):
            return child.get("url"), child.get("type") or ""
    for child in item:
        if _localname(child.tag) == "content" and child.get("url"):
            mime = child.get("type") or ""
            if mime.startswith("audio/") or child.get("medium") == "audio":
                return child.get("url"), mime
    for child in item:
        if (_localname(child.tag) == "link" and child.get("rel") == "enclosure"
                and child.get("href")):
            return child.get("href"), child.get("type") or ""
    return "", ""


def _item_link(item) -> str:
    link = _first_text(item, "link")
    if link:
        return link
    for child in item:  # atom: <link href=…/> with no/alternate rel
        if (_localname(child.tag) == "link" and child.get("href")
                and child.get("rel") in (None, "alternate")):
            return child.get("href")
    return ""


_XML_PREDEFINED = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _repair_entities(text: str) -> str:
    def _entity(mm):
        name = mm.group(1)
        if name in _XML_PREDEFINED:
            return mm.group(0)
        ch = html.entities.html5.get(name + ";")
        # unknown entities stay visible as literal text, never dropped
        return "".join(f"&#{ord(c)};" for c in ch) if ch else "&amp;" + name + ";"
    text = re.sub(r"&([A-Za-z][A-Za-z0-9]{1,31});", _entity, text)
    return re.sub(r"&(?![A-Za-z][A-Za-z0-9]{1,31};|#\d+;|#x[0-9A-Fa-f]+;)", "&amp;", text)


def _sanitize_xml(raw: bytes) -> bytes:
    """Best-effort repair of common real-world feed dirt: junk before the
    declaration, control chars, undefined HTML entities, bare '&'. CDATA
    sections pass through untouched. Known limitation: a bare '&' that spells
    an HTML entity name inside a URL query (\u2026?id=1&sect;ion=2) is converted
    like an entity \u2014 unfixable without a full parser."""
    head = raw[:64]
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        enc = "utf-16"                        # BOM decides the byte order
    elif b"\x00" in head:                     # null-interleaved: BOM-less UTF-16
        enc = "utf-16-be" if head.startswith(b"\x00") else "utf-16-le"
    else:
        m = re.search(rb'<\?xml[^>]*encoding=["\']([A-Za-z0-9._-]+)["\']', raw[:200])
        enc = m.group(1).decode("ascii", "replace") if m else "utf-8"
    try:
        text = raw.decode(enc, "replace")
    except LookupError:                       # unknown codec name in declaration
        text = raw.decode("utf-8", "replace")
    text = text.lstrip("\ufeff\x00 \t\r\n")
    # we re-encode as UTF-8 below, so the declared encoding must not disagree
    text = re.sub(r'(<\?xml[^>]*?)\s+encoding=["\'][^"\']*["\']', r"\1", text, count=1)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    parts = re.split(r"(<!\[CDATA\[.*?\]\]>)", text, flags=re.S)
    return "".join(p if p.startswith("<![CDATA[") else _repair_entities(p)
                   for p in parts).encode("utf-8")


def _parse_xml(raw: bytes) -> "ET.Element":
    try:
        return ET.fromstring(raw)
    except ET.ParseError as err:
        try:
            return ET.fromstring(_sanitize_xml(raw))
        except ET.ParseError:
            raise err from None             # fail loudly with the original error


def parse_feed(feed_url: str) -> tuple[str, str, list[Episode]]:
    """-> (show_title, show_author, episodes). Handles RSS 2.0, RSS 1.0 (RDF)
    and Atom, with any namespace layout (matching by localname)."""
    root = _parse_xml(fetch(feed_url).read())
    chan = next((el for el in root.iter()
                 if _localname(el.tag) in ("channel", "feed")), root)
    eps: list[Episode] = []
    for it in root.iter():
        if _localname(it.tag) not in ("item", "entry"):
            continue
        url, mime = _find_enclosure(it)
        if not url:
            continue
        eps.append(Episode(
            title=_first_text(it, "title"),
            pub=_pub(it),
            url=url,
            mime=mime,
            guid=_first_text(it, "guid", "id"),
            link=_item_link(it),
        ))
    return _first_text(chan, "title"), _author(chan), eps


def resolve_show(src: str) -> Show:
    kind, s = classify(src)
    if kind == "apple_show":
        feed, name, author, pid = apple_show_to_feed(s)
    elif kind == "rss":
        feed, name, author, pid = s, "", "", ""
    else:
        raise ValueError(f"{src!r} is an episode link, not a show; use get() directly")
    title, feed_author, eps = parse_feed(feed)
    return Show(title=name or title, feed=feed, author=author or feed_author,
                apple_id=pid, episodes=eps)


# --------------------------------------------------------------------------- #
# direct-episode resolvers (pasted links)
# --------------------------------------------------------------------------- #
def xyz_episode_to_audio(src: str) -> tuple[str, str]:
    html = fetch(src).read().decode("utf-8", "replace")
    m = re.search(r'<meta\s+property="og:audio"\s+content="([^"]+)"', html) \
        or re.search(r"(https://media\.xyzcdn\.net/[^\"'\s]+\.m4a)", html)
    if not m:
        raise ValueError("Could not find og:audio on xiaoyuzhou page")
    t = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
    return m.group(1), (t.group(1).strip() if t else "episode")


def apple_episode_to_audio(
    src: str,
) -> tuple[str | None, str | None, str | None, str]:
    """Resolve .../id<show>?i=<trackId> via the show's episode list.
    Returns (url, title, releaseDate, mime) or (None, None, None, "") to signal a
    deep-catalog miss (caller may fall back to yt-dlp). `mime` is derived from
    Apple's episodeContentType/episodeFileExtension so the saved file gets the
    right extension even when episodeUrl is an extension-less redirect."""
    show_m = re.search(r"/id(\d+)", src)
    track_m = re.search(r"[?&]i=(\d+)", src)
    if show_m and track_m:
        q = urllib.parse.urlencode(
            {"id": show_m.group(1), "entity": "podcastEpisode", "limit": 200})
        for r in fetch_json(f"{ITUNES_LOOKUP}?{q}").get("results", []):
            if str(r.get("trackId")) == track_m.group(1) and r.get("episodeUrl"):
                ext = (r.get("episodeFileExtension") or "").lower()
                ctype = (r.get("episodeContentType") or "").lower()
                # Map Apple's hints onto a mime string ext_for() understands.
                if ext in ("m4a", "mp4", "aac") or "mp4" in ctype:
                    mime = "audio/mp4"
                elif ext == "mp3" or "mpeg" in ctype:
                    mime = "audio/mpeg"
                else:
                    mime = ctype or ""
                return (r["episodeUrl"], (r.get("trackName") or "").strip(),
                        r.get("releaseDate"), mime)
    return None, None, None, ""


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def select(episodes: list[Episode], *, match: str | None = None,
           latest: int | None = None, index: str | None = None) -> list[Episode]:
    if match:
        rx = re.compile(match, re.I)
        return [e for e in episodes if rx.search(e.title)]
    if latest:
        return episodes[:latest]
    if index is not None:
        idxs = [int(x) for x in str(index).split(",") if x.strip() != ""]
        return [episodes[i] for i in idxs if -len(episodes) <= i < len(episodes)]
    return []


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
# OS-forbidden characters (Windows is the strictest) + control chars.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Unicode categories to drop: emoji & other symbols, modifier symbols,
# format/control/surrogate/private-use/unassigned.
_DROP_CATEGORIES = frozenset({"So", "Sk", "Cf", "Cc", "Cs", "Co", "Cn"})
# Windows reserved device names — illegal as a filename base even with an extension.
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)),
                       *(f"LPT{i}" for i in range(1, 10))})


def safe_filename(name: str, maxlen: int = 120) -> str:
    """Normalize a title into a cloud-/filesystem-safe name.

    Folds full-width & compatibility forms (NFKC), drops emoji/symbols and
    control/format characters, replaces OS-forbidden characters, collapses
    whitespace, and trims leading/trailing dots, dashes and spaces. Letters
    (including CJK), digits, spaces and ordinary punctuation are preserved so
    files upload cleanly to Google Drive / OneDrive / Dropbox / iCloud, etc.
    """
    name = unicodedata.normalize("NFKC", name)
    name = "".join(" " if unicodedata.category(c) in _DROP_CATEGORIES else c
                   for c in name)
    name = _FORBIDDEN.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    name = name[:maxlen].strip(" .-_") or "untitled"
    # Avoid Windows reserved device names (CON, NUL, COM1…), incl. "CON.mp3".
    if name.split(".", 1)[0].strip().upper() in _RESERVED:
        name = "_" + name
    return name


def ext_for(url: str, mime: str) -> str:
    if "mp4" in mime or url.lower().split("?")[0].endswith((".m4a", ".mp4", ".aac")):
        return ".m4a"
    return ".mp3"


# 1 MiB read buffer — fewer syscalls than 256 KiB, meaningfully faster on fast links.
_BUF_SIZE = 1024 * 1024
_PROGRESS_INTERVAL = 1024 * 1024
# Segment sizing: don't split unless it actually pays off.
_MIN_SEGMENT = 4 * 1024 * 1024
_MAX_THREADS = 16
# A tiny (1-byte) Range GET is enough to learn "Range supported?" + total size.
_PROBE_SIZE = 1


def _probe_url(url: str) -> int:
    """Probe total size. Tries HEAD first; if the CDN rejects HEAD (403/405 —
    common on podcast CDNs), falls back to a 1-byte Range GET so we still learn
    the size instead of giving up (which would force a needless full re-download).
    Returns content_length (0 if unknown)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        cl = resp.headers.get("Content-Length")
        resp.close()
        if cl:
            return int(cl)
    except Exception:
        pass
    # HEAD unavailable/blocked or no length -> derive size from a 1-byte Range GET.
    ok, total = _probe_range_and_size(url)
    return total if ok else 0


def _parse_content_range_total(cr: str) -> int:
    """Extract total size from a Content-Range header like 'bytes 0-8191/1234567'."""
    m = re.search(r"/(\d+)$", cr or "")
    return int(m.group(1)) if m else 0


def _probe_range_and_size(url: str) -> tuple[bool, int]:
    """Probe Range support + total size with one small GET (256KB).

    Returns (range_supported, total_size).
    """
    headers = {"User-Agent": UA, "Range": f"bytes=0-{_PROBE_SIZE - 1}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except Exception:
        return False, 0
    status = getattr(resp, "status", 200)
    resp.read()
    resp.close()
    if status != 206:
        return False, 0
    total = _parse_content_range_total(resp.headers.get("Content-Range", ""))
    return True, total


def _download_range_with_progress(url: str, dest: str, start: int, end: int,
                                  progress_list: list, progress_lock: threading.Lock,
                                  index: int, total_size: int, on_progress=None,
                                  resume: bool = True) -> None:
    """Download exactly bytes [start, end] into `dest` (one segment).

    Enforces the exact segment length: raises IOError on a short read so a
    truncated segment surfaces as a failure instead of being silently merged
    into a corrupt file. Only resumes a partial `dest` when `resume` is True;
    the caller guarantees `dest` was left over from an identical segment plan.
    """
    seg_len = end - start + 1
    existing = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
    if existing >= seg_len:
        # This segment is already fully present (resume of a matching plan).
        with progress_lock:
            progress_list[index] = seg_len
            total_done = sum(progress_list)
        if on_progress:
            on_progress(total_done, total_size)
        return
    if not resume and os.path.exists(dest):
        os.remove(dest)
        existing = 0

    headers = {"User-Agent": UA, "Range": f"bytes={start + existing}-{end}"}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:  # already satisfied
            with progress_lock:
                progress_list[index] = seg_len
            return
        raise

    mode = "ab" if existing else "wb"
    written = existing
    last_report = 0
    try:
        with open(dest, mode) as f:
            while True:
                buf = resp.read(_BUF_SIZE)
                if not buf:
                    break
                f.write(buf)
                written += len(buf)
                if written - last_report >= _PROGRESS_INTERVAL:
                    last_report = written
                    with progress_lock:
                        progress_list[index] = written
                        total_done = sum(progress_list)
                    if on_progress:
                        on_progress(total_done, total_size)
    finally:
        resp.close()

    if written != seg_len:
        raise IOError(
            f"segment {index} short read: got {written} bytes, expected {seg_len} "
            f"(server closed early?)"
        )
    with progress_lock:
        progress_list[index] = written
        total_done = sum(progress_list)
    if on_progress:
        on_progress(total_done, total_size)


def _cleanup_parts(part_files: list[str], plan_path: str | None = None) -> None:
    """Remove any leftover part files (and an optional plan sidecar)."""
    for pf in part_files:
        try:
            os.remove(pf)
        except OSError:
            pass
    if plan_path:
        try:
            os.remove(plan_path)
        except OSError:
            pass


def _merge_parts(part_files: list[str], dest: str,
                 expected_total: int | None = None) -> None:
    """Merge part files in order into dest, then remove part files.

    If `expected_total` is given, verifies the merged size matches; on mismatch
    the corrupt output is removed and IOError is raised so a truncated/misaligned
    download never masquerades as a finished file.
    """
    written = 0
    with open(dest, "wb") as out:
        for pf in part_files:
            with open(pf, "rb") as f:
                while True:
                    buf = f.read(8 * 1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
    if expected_total is not None and written != expected_total:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise IOError(
            f"merged size {written} != expected {expected_total}; download corrupt"
        )
    for pf in part_files:
        try:
            os.remove(pf)
        except OSError:
            pass


def download_url(url: str, dest: str, *, resume: bool = True, on_progress=None,
                 threads: int = 1) -> str:
    """Stream a URL to dest with Range-based resume. Stdlib only.

    on_progress, if given, is called as on_progress(downloaded_bytes, total_bytes)
    after each chunk; total_bytes is 0 when the server doesn't report a length.

    threads > 1 enables multi-threaded segmented download. Falls back to single
    thread if the server doesn't support Range or the file is too small.
    """
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)

    if threads <= 1:
        return _download_url_single(url, dest, resume=resume, on_progress=on_progress)

    if os.path.exists(dest) and resume:
        existing_size = os.path.getsize(dest)
        if existing_size > 0:
            cl = _probe_url(url)
            if cl > 0 and existing_size >= cl:
                if on_progress:
                    on_progress(cl, cl)
                return dest

    range_ok, total_size = _probe_range_and_size(url)
    if not range_ok or total_size < _MIN_SEGMENT * 2:
        return _download_url_single(url, dest, resume=resume, on_progress=on_progress)

    n_threads = min(threads, _MAX_THREADS, max(2, total_size // _MIN_SEGMENT))
    part_size = total_size // n_threads

    part_files = [f"{dest}.part{i}" for i in range(n_threads)]
    plan_path = f"{dest}.plan"

    # Resume is only safe if the leftover parts came from an *identical* segment
    # plan (same total size + thread count). Otherwise the byte ranges differ and
    # resuming would splice mismatched data into a corrupt file. The plan sidecar
    # records the plan; a mismatch (or --no-resume) means start clean.
    plan_key = f"{total_size}:{n_threads}"
    can_resume = False
    if resume and os.path.exists(plan_path):
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                can_resume = f.read().strip() == plan_key
        except OSError:
            can_resume = False
    if not can_resume:
        _cleanup_parts(part_files, plan_path)
    try:
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write(plan_key)
    except OSError:
        pass

    progress_list = [0] * n_threads
    progress_lock = threading.Lock()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
        futures = []
        for i in range(n_threads):
            seg_start = i * part_size
            seg_end = total_size - 1 if i == n_threads - 1 else (i + 1) * part_size - 1
            futures.append(executor.submit(
                _download_range_with_progress, url, part_files[i], seg_start, seg_end,
                progress_list, progress_lock, i, total_size, on_progress, can_resume
            ))
        # On a segment failure we KEEP the good parts + plan sidecar: the next run
        # with the same plan safely resumes them (the plan guard prevents reusing
        # misaligned parts). Only a merge-size mismatch — which means the parts are
        # inconsistent — triggers a full cleanup below.
        for fut in concurrent.futures.as_completed(futures):
            fut.result()

    try:
        _merge_parts(part_files, dest, expected_total=total_size)
    except Exception:
        _cleanup_parts(part_files, plan_path)
        raise

    _cleanup_parts([], plan_path)
    if on_progress:
        on_progress(total_size, total_size)
    return dest


def _download_url_single(url: str, dest: str, *, resume: bool = True,
                         on_progress=None) -> str:
    """Single-threaded download (original implementation)."""
    headers = {"User-Agent": UA}
    existing = os.path.getsize(dest) if (resume and os.path.exists(dest)) else 0
    if existing:
        headers["Range"] = f"bytes={existing}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return dest
        raise
    try:
        if existing and getattr(resp, "status", 200) == 200:
            existing = 0                # server ignored Range; restart cleanly
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype.startswith("text/"):
            # e.g. Ximalaya's CDN answers a stale enclosure query with 200 text/plain
            raise ValueError(f"server returned {ctype}, not audio — "
                             "the feed's enclosure URL may be stale")
        mode = "ab" if existing else "wb"
        remaining = resp.length or 0
        total = (remaining + existing) if remaining else 0
        done = existing
        with open(dest, mode) as f:
            while True:
                buf = resp.read(_BUF_SIZE)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if on_progress:
                    on_progress(done, total)
    finally:
        resp.close()
    return dest


def download_episode(ep: Episode, out_dir: str, **kw) -> str:
    stem = safe_filename(f"{ep.date} - {ep.title}")
    dest = os.path.join(out_dir, stem + ext_for(ep.url, ep.mime))
    return download_url(ep.url, dest, **kw)
