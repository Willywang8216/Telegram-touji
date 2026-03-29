import os
import re
import tempfile
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


def _sanitize_netscape_cookies_text(text: str) -> str | None:
    """Best-effort sanitize a cookies.txt into Netscape format.

    yt-dlp is strict about cookiefile parsing and can error out if any line is malformed.
    """

    if not text:
        return None

    out_lines: list[str] = []
    saw_header = False

    for raw_line in str(text).splitlines():
        line = raw_line.strip("\r\n")
        if not line:
            continue

        if line.startswith("#"):
            if "Netscape HTTP Cookie File" in line:
                saw_header = True
            out_lines.append(line)
            continue

        parts = line.split("\t")
        if len(parts) < 7:
            parts = re.split(r"\s+", line)

        if len(parts) < 7:
            continue

        out_lines.append("\t".join(parts[:7]))

    if not out_lines:
        return None

    if not saw_header:
        out_lines.insert(0, "# Netscape HTTP Cookie File")

    return "\n".join(out_lines) + "\n"


def _prepare_cookiefile(cookies_file: str, *, logger=None) -> str | None:
    p = Path(str(cookies_file))
    if not p.exists():
        return None

    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return None

    sanitized = _sanitize_netscape_cookies_text(raw)
    if not sanitized:
        return None

    tmp = tempfile.NamedTemporaryFile("w", delete=False, prefix="twitter_cookies_", suffix=".txt")
    try:
        tmp.write(sanitized)
        tmp.close()
        return tmp.name
    except Exception:  # noqa: BLE001
        try:
            tmp.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.unlink(tmp.name)
        except Exception:  # noqa: BLE001
            pass
        return None


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

    prepared_cookiefile: str | None = None
    if cookies_file:
        prepared_cookiefile = _prepare_cookiefile(str(cookies_file), logger=logger)

    ydl_opts: dict = {
        "quiet": True,
        "noprogress": True,
        "outtmpl": str(out_dir / "%(id)s_%(autonumber)03d.%(ext)s"),
        "nocheckcertificate": True,
        "ignoreerrors": False,
        "retries": 3,
    }
    if prepared_cookiefile:
        ydl_opts["cookiefile"] = str(prepared_cookiefile)
    elif cookies_file and logger is not None:
        logger.info("[twitter] cookies_file_invalid_or_unreadable: %s", str(cookies_file))

    if logger is not None:
        # Provide minimal visibility without depending on structured logging.
        # yt-dlp expects logger methods with signature (self, msg, *args).
        def _log(self, msg: str, *args) -> None:  # noqa: ANN001
            try:
                if args:
                    msg = str(msg) % args
                logger.info(msg)
            except Exception:  # noqa: BLE001
                return

        ydl_opts["logger"] = type("_YdlLogger", (), {"debug": _log, "warning": _log, "error": _log})()

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    finally:
        if prepared_cookiefile:
            try:
                os.unlink(prepared_cookiefile)
            except Exception:  # noqa: BLE001
                pass

    after = [p for p in out_dir.iterdir() if p.is_file() and p.name not in before]
    after.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return after[: max(0, int(max_files or 0))]
