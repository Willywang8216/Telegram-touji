import re
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
    """Build a list of profile URLs to try with yt-dlp.

    yt-dlp support varies by version (x.com vs twitter.com; /media vs base timeline).
    We try a small set of common variants.
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


def list_profile_tweets(
    profile: str,
    *,
    cookies_file: str | None = None,
    limit: int = 30,
) -> list[dict[str, str]]:
    """List recent tweets from a profile.

    Returns a list of {url, text} where url is a normalized tweet URL.

    Uses yt-dlp's twitter extractor. This is best-effort: X frequently changes.
    """

    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("yt-dlp is required to list tweets") from exc

    limit = max(1, int(limit or 1))

    ydl_opts: dict[str, Any] = {
        "quiet": True,
        "noprogress": True,
        "extract_flat": True,
        "skip_download": True,
        "playlistend": limit,
        "nocheckcertificate": True,
        "retries": 3,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = str(cookies_file)

    last_exc: Exception | None = None

    candidates = candidate_profile_urls(profile, media_only=True)
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
