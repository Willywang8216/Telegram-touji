import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from twitter_expand import extract_tweet_urls


_TWEET_ID_RE = re.compile(r"/status/(\d+)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def tweet_id_from_url(url: str) -> str | None:
    m = _TWEET_ID_RE.search(str(url or ""))
    if not m:
        return None
    return m.group(1)


def normalize_twitter_profile_url(profile: str, *, media_only: bool = True) -> str:
    s = str(profile or "").strip().strip("<>")
    if not s:
        raise ValueError("empty_profile")

    if s.startswith("@"):  # allow @handle
        s = s[1:]

    if s.startswith("http://"):
        s = "https://" + s[len("http://") :]

    if not s.startswith("https://"):
        s = "https://x.com/" + s.lstrip("/")

    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")

    # Normalize host.
    s = re.sub(r"^https://(?:www\.)?(?:mobile\.)?twitter\.com/", "https://x.com/", s, flags=re.IGNORECASE)
    s = re.sub(r"^https://(?:www\.)?x\.com/", "https://x.com/", s, flags=re.IGNORECASE)

    if media_only and not s.endswith("/media"):
        s = s + "/media"

    return s


def _extract_tweet_entries(info: dict[str, Any]) -> list[dict[str, str]]:
    entries = info.get("entries") or []
    out: list[dict[str, str]] = []

    for e in entries:
        if not e:
            continue

        candidates: list[str] = []
        for k in ("webpage_url", "original_url", "url"):
            v = e.get(k)
            if v:
                candidates.append(str(v))

        tid = str(e.get("id") or "")
        uploader = str(e.get("uploader_id") or e.get("uploader") or "").lstrip("@").strip()
        if tid.isdigit() and uploader:
            candidates.append(f"https://x.com/{uploader}/status/{tid}")

        normalized = extract_tweet_urls("\n".join(candidates))
        if not normalized:
            continue

        text = str(e.get("description") or e.get("title") or "")
        out.append({"url": normalized[0], "text": text})

    return out


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", str(s or ""))


def candidate_profile_urls(profile: str, *, media_only: bool = True) -> list[str]:
    """Build a list of profile URLs to try.

    Downstream scraping support varies by runtime (x.com vs twitter.com; /media vs base timeline),
    so we try a small set of common variants.
    """

    base = normalize_twitter_profile_url(profile, media_only=False)
    base = base.rstrip("/")
    if base.endswith("/media"):
        base = base[: -len("/media")]

    bases: list[str] = []

    # Most of the time normalize_twitter_profile_url returns https://x.com/<handle>...
    if base.startswith("https://x.com/"):
        tail = base[len("https://x.com/") :]
        handle = (tail.split("/", 1)[0] if tail else "").strip()
        if handle and handle not in {"i", "home", "search", "intent"}:
            bases = [f"https://x.com/{handle}", f"https://twitter.com/{handle}"]

    if not bases:
        bases = [
            base,
            base.replace("https://x.com/", "https://twitter.com/"),
            base.replace("https://twitter.com/", "https://x.com/"),
        ]

    out: list[str] = []
    for b in bases:
        b = str(b or "").rstrip("/")
        if not b:
            continue

        if media_only:
            out.append(b + "/media")
        out.append(b)

    # De-dupe while preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u in seen:
            continue
        seen.add(u)
        uniq.append(u)

    return uniq


def _cookies_header_from_netscape_file(path: str) -> str | None:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None

    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip("\n")
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue

        domain = str(parts[0] or "").lstrip(".").casefold()
        if "twitter.com" not in domain and "x.com" not in domain:
            continue

        name = (parts[5] or "").strip()
        value = (parts[6] or "").strip()
        if not name:
            continue
        cookies[name] = value

    if not cookies:
        return None

    return "; ".join([f"{k}={v}" for k, v in cookies.items()])


_REL_TWEET_HREF_RE = re.compile(r"href=\"(?P<href>/[^\"<>\s]*/status/\d+[^\"<>\s]*)\"")
_DATA_TWEET_ID_RE = re.compile(r"data-tweet-id=\"(?P<id>\d+)\"")


def _extract_tweet_urls_from_html(html: str, *, base_url: str) -> list[str]:
    out: list[str] = []

    # 1) Relative hrefs in DOM order.
    u = urllib.parse.urlparse(base_url)
    prefix = f"{u.scheme}://{u.netloc}"

    for m in _REL_TWEET_HREF_RE.finditer(html or ""):
        href = m.group("href")
        if not href:
            continue
        out.extend(extract_tweet_urls(prefix + href))

    # 2) Full URLs already in the document.
    out.extend(extract_tweet_urls(html))

    # De-dupe while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)

    return uniq


def _extract_handle_from_profile_url(url: str) -> str | None:
    u = urllib.parse.urlparse(str(url or ""))
    path = (u.path or "").strip("/")
    if not path:
        return None
    head = path.split("/", 1)[0].strip()
    if not head or head in {"i", "home", "search", "intent", "explore"}:
        return None
    return head


def _syndication_profile_url(handle: str) -> str:
    # Public endpoint often used by embeddable timelines.
    return f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"


def _tweet_urls_from_syndication_html(html: str, *, handle: str) -> list[str]:
    out: list[str] = []

    # The syndication HTML typically contains data-tweet-id attributes.
    for m in _DATA_TWEET_ID_RE.finditer(html or ""):
        tid = m.group("id")
        if tid:
            out.append(f"https://x.com/{handle}/status/{tid}")

    out.extend(extract_tweet_urls(html))

    # De-dupe while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        x = str(x or "").strip()
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        uniq.append(x)

    return uniq


def _list_profile_tweets_via_html(
    profile_url: str,
    *,
    cookies_file: str | None,
    limit: int,
) -> list[dict[str, str]]:
    cookie_header = _cookies_header_from_netscape_file(str(cookies_file)) if cookies_file else None
    if not cookie_header:
        raise RuntimeError("empty_cookies")

    req = urllib.request.Request(
        profile_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": cookie_header,
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            final_url = getattr(resp, "url", None) or profile_url
    except urllib.error.HTTPError as exc:  # pragma: no cover
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:  # pragma: no cover
        raise RuntimeError("url_error") from exc

    html = body.decode("utf-8", errors="ignore")

    # Common unauthenticated redirect pages contain this path.
    if "/i/flow/login" in html or "/login" in str(final_url):
        raise RuntimeError("not_authenticated")

    urls = _extract_tweet_urls_from_html(html, base_url=str(final_url))

    if not urls:
        # Fallback: syndication endpoint used by embedded timelines.
        handle = _extract_handle_from_profile_url(str(final_url))
        if handle:
            syndication_url = _syndication_profile_url(handle)
            req2 = urllib.request.Request(
                syndication_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cookie": cookie_header,
                },
                method="GET",
            )

            with urllib.request.urlopen(req2, timeout=20) as resp2:
                body2 = resp2.read()
            html2 = body2.decode("utf-8", errors="ignore")
            urls = _tweet_urls_from_syndication_html(html2, handle=handle)

    if not urls:
        raise RuntimeError("no_tweet_urls_found")

    urls = urls[: max(1, int(limit or 1))]

    return [{"url": u, "text": ""} for u in urls]


def list_profile_tweets(
    profile: str,
    *,
    cookies_file: str | None = None,
    limit: int = 30,
) -> list[dict[str, str]]:
    """List recent tweets from a profile.

    Returns a list of {url, text} where url is a normalized tweet URL.

    Notes:
    - yt-dlp generally supports tweet URLs, but many versions do *not* support profile URLs.
    - We therefore try a lightweight HTML scrape first when cookies are available, and only
      fall back to yt-dlp when we have no cookies.
    """

    limit = max(1, int(limit or 1))

    candidates = candidate_profile_urls(profile, media_only=True)

    # Preferred path: HTML scrape using auth cookies.
    if cookies_file and Path(str(cookies_file)).exists():
        last_exc: Exception | None = None
        for url in candidates:
            try:
                res = _list_profile_tweets_via_html(url, cookies_file=cookies_file, limit=limit)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
            if res:
                return res

        # If all attempts errored, surface a useful error.
        if last_exc is not None:
            tried = ", ".join(candidates)
            raise RuntimeError(f"Failed to list tweets for {profile}. Last error: {last_exc}. Tried: {tried}") from last_exc

        # No errors, just no tweets.
        return []

    # Fallback: yt-dlp (only when no cookies are provided).
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("yt-dlp is required to list tweets") from exc

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "nocheckcertificate": True,
        "retries": 3,
    }

    last_exc = None
    for url in candidates:
        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)

            if not isinstance(info, dict):
                continue

            return _extract_tweet_entries(info)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue

    if last_exc is not None:
        msg = _strip_ansi(str(last_exc))
        tried = ", ".join(candidates)
        raise RuntimeError(f"Failed to list tweets for {profile}. Last error: {msg}. Tried: {tried}") from last_exc

    return []


def ensure_parent_dir(path: str | Path) -> None:
    p = Path(path)
    if p.parent and str(p.parent) not in {"", "."}:
        p.parent.mkdir(parents=True, exist_ok=True)
