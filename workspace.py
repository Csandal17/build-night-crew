"""The per-run library: sources that agents find, saved by code.

Researchers only *propose* web pages. This module fetches each page itself and saves what
the page actually said, so a quote can later be checked against the real text and not against
a model's retelling of it. One folder per run: workspace/run_<id>/sources/.

Files use the same format as sources/: header lines, then `---`, then the text.

Safety: URLs come from a model (or from web pages it read), so a fetch only goes to public
internet hosts, never to localhost or private networks, and every redirect is re-checked.
"""

import ipaddress
import re
import socket
import threading
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

HERE = Path(__file__).parent
WORKSPACE_DIR = HERE / "workspace"
PACK_DIR = HERE / "sources"           # the hand-written NHS pack, optional base library

MAX_PAGE_BYTES = 1_500_000            # stop reading a page after this much
MAX_TEXT_CHARS = 12_000               # keep this much of the text of each page
MIN_TEXT_CHARS = 200                  # shorter than this is not a usable source
FETCH_TIMEOUT = 15.0
MAX_REDIRECTS = 3
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/json")

_save_lock = threading.Lock()


class FetchError(Exception):
    """A page couldn't be fetched, or isn't a usable source."""


# ---------- fetching ----------

def is_public_url(url):
    """True only for http(s) URLs whose host resolves to public internet addresses."""
    try:
        parts = urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return False
        if parts.port not in (None, 80, 443):
            return False
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except (ValueError, OSError):
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified):
            return False
    return bool(infos)


def _get(url):
    """Fetch one URL and return (final_url, content_type, body bytes). Redirects are followed
    by hand so each hop is checked. Tests replace this function."""
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False,
                      headers={"User-Agent": "build-night-crew/1.0 (research agent)"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_url(url):
                raise FetchError(f"{url} is not a public web address.")
            with client.stream("GET", url) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise FetchError(f"{url} returned {response.status_code}.")
                kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_PAGE_BYTES:
                        break
                return url, kind, body
    raise FetchError(f"Too many redirects for {url}.")


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "nav", "footer", "form"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract_text(content, kind="text/html"):
    """(title, readable text) from a page's raw content."""
    if kind in ("text/html", "application/xhtml+xml"):
        parser = _TextExtractor()
        parser.feed(content)
        raw = "".join(parser.parts)
        title = " ".join(parser.title.split())
    else:
        raw, title = content, ""
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return title, "\n".join(line for line in lines if line)


def fetch_page(url):
    """Fetch a public page and return {"url", "title", "text", "retrieved"}. Raises FetchError."""
    try:
        final_url, kind, body = _get(url)
    except FetchError:
        raise
    except httpx.HTTPError as error:
        raise FetchError(f"{url} could not be fetched ({type(error).__name__}).") from error
    if kind and not kind.startswith(TEXT_TYPES):
        raise FetchError(f"{url} is a {kind} file, not a web page or text.")
    title, text = extract_text(body.decode("utf-8", errors="replace"), kind or "text/html")
    text = text[:MAX_TEXT_CHARS]
    if len(text) < MIN_TEXT_CHARS:
        raise FetchError(f"{url} had too little readable text to use as a source.")
    return {"url": final_url, "title": title or final_url, "text": text,
            "retrieved": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ---------- the library on disk ----------

def library_dir(run_id):
    path = WORKSPACE_DIR / f"run_{run_id}" / "sources"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _publisher(url):
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "source"


def _parse(path):
    header, _, text = path.read_text(encoding="utf-8").partition("\n---\n")
    meta = dict(line.split(": ", 1) for line in header.splitlines() if ": " in line)
    return {**meta, "text": text.strip()}


def save_source(run_id, page):
    """Save a fetched page into the run's library. Returns its source id (S1, S2, ...).
    The same URL is only saved once."""
    folder = library_dir(run_id)
    with _save_lock:
        files = sorted(folder.glob("src-*.txt"))
        for number, path in enumerate(files, start=1):
            if _parse(path).get("url") == page["url"]:
                return f"S{number}"
        number = len(files) + 1
        header = (f"title: {page['title']}\npublisher: {_publisher(page['url'])}\n"
                  f"url: {page['url']}\nretrieved: {page.get('retrieved', '')}\n---\n")
        path = folder / f"src-{number:03d}-{_slug(page['title'])}.txt"
        path.write_text(header + page["text"], encoding="utf-8")
        return f"S{number}"


def import_pack(run_id):
    """Copy the hand-written pack in sources/ into the run's library. Returns the ids it got."""
    ids = []
    for path in sorted(PACK_DIR.glob("*.txt")):
        meta = _parse(path)
        page = {"url": meta.get("url") or f"pack:{path.name}", "title": meta.get("title", path.stem),
                "text": meta["text"], "retrieved": meta.get("page last reviewed", "")}
        ids.append(save_source(run_id, page))
    return ids


def load_library(run_id):
    """Everything saved for this run as {"S1": {title, publisher, url, retrieved, text}, ...}.
    Ids follow the order sources were saved, so an id never changes once handed out."""
    folder = library_dir(run_id)
    return {f"S{n}": _parse(path) for n, path in enumerate(sorted(folder.glob("src-*.txt")), start=1)}


# ---------- quote checking ----------

def normalise(text):
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    return " ".join(text.lower().split())


def quote_in_source(quote, source):
    quote = normalise(quote)
    return bool(quote) and quote in normalise(source["text"])
