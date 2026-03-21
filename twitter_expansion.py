"""twitter_expansion.py

Backward-compatible module name.

The project originally used `twitter_expansion.py`, but the implementation lives
in `twitter_expand.py`. Tests (and potentially downstream users) still reference
this filename, so we keep it as a small re-export shim.
"""

from twitter_expand import download_tweet_media, extract_tweet_urls

__all__ = ["download_tweet_media", "extract_tweet_urls"]
