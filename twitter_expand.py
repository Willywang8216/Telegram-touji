import re
from pathlib import Path


_TWEET_URL_RE = re.compile(
    r"https?://(?:www\.)?((?:mobile\.)?twitter\.com|x\.com)/(i/web|[A-Za-z0-9_]{1,20})/status/(\d+)(?:/[^\s]*)?",
    re.IGNORECASE,
)


def extract_tweet_urls(text: str) -> list[str]:
    """Extract and normalize tweet URLs from a text.

    Normalization rules (as used by the relay bot):
    - Strip query params/fragments and trailing punctuation.
    - Strip trailing path segments like /photo/1.
    - Keep the original host (twitter.com vs x.com).
    """

    raw = text or ""
    seen: set[str] = set()
    out: list[str] = []

    for m in _TWEET_URL_RE.finditer(raw):
        host = (m.group(1) or "").lower()
        if host.endswith("twitter.com"):
            host = "twitter.com"

        segment = m.group(2) or ""
        tweet_id = m.group(3)

        if segment.lower() == "i/web":
            base_path = f"i/web/status/{tweet_id}"
        else:
            base_path = f"{segment}/status/{tweet_id}"

        url = f"https://{host}/{base_path}"

        if url not in seen:
            seen.add(url)
            out.append(url)

    return out


def download_tweet_media(
    url: str,
    output_dir: str | Path,
    *,
    cookies_file: str | None = None,
    max_files: int = 8,
    logger=None,
) -> list[Path]:
    """Download tweet media with yt-dlp.

    This function is intentionally imported safely (yt_dlp is imported lazily)
    so that unit tests can import the module without requiring yt-dlp.
    """

    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("yt-dlp is required to download tweet media") from exc

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    before = {p.name for p in out_dir.iterdir()} if out_dir.exists() else set()

    ydl_opts: dict = {
        "quiet": True,
        "noprogress": True,
        "outtmpl": str(out_dir / "%(id)s_%(autonumber)03d.%(ext)s"),
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "retries": 3,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = str(cookies_file)

    if logger is not None:
        # Provide minimal visibility without depending on structured logging.
        def _log(msg: str) -> None:
            try:
                logger.info(msg)
            except Exception:  # noqa: BLE001
                return

        ydl_opts["logger"] = type("_YdlLogger", (), {"debug": _log, "warning": _log, "error": _log})()

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    after = [p for p in out_dir.iterdir() if p.is_file() and p.name not in before]
    after.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return after[: max(0, int(max_files or 0))]
