set -eu

# Best-effort: keep yt-dlp fresh (X/Twitter breaks older versions frequently).
python -m pip install -U "yt-dlp>=2026.2.21" || true

exec python telegram_bot.py
