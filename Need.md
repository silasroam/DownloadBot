Technical Specification: Pyrogram + yt-dlp Telegram Downloader Bot (Arch Linux Build)
1. System Requirements & Environment (Arch Linux)
OS & System Packages

    OS: Arch Linux (rolling)

    Core Dependencies:
    Bash

    sudo pacman -S python ffmpeg aria2

2. Project File Structure
Plaintext

tg-media-downloader/
├── .env.example          # Template for environment variables
├── .gitignore            # Git ignore rules
├── requirements.txt      # Python dependencies
├── main.py               # Entry point
│
├── bot/
│   ├── __init__.py
│   ├── client.py         # Pyrogram client init
│   ├── handlers.py       # Commands & URL regex handlers
│   └── keyboards.py      # Inline buttons
│
├── services/
│   ├── __init__.py
│   ├── downloader.py     # Async yt-dlp executor
│   └── cleaner.py        # Storage cleanup logic
│
└── downloads/            # Temp storage (git-ignored)

3. Environment Variables (.env.example)
Ini, TOML

API_ID=12345678
API_HASH=your_api_hash_here
BOT_TOKEN=your_bot_token_here
SESSION_NAME=downloader_bot
MAX_FILE_SIZE_MB=2000
DOWNLOAD_DIR=downloads/
# HTTP_PROXY=http://user:pass@ip:port

4. Dependencies (requirements.txt)
Plaintext

Pyrogram>=2.0.106
TgCrypto>=1.2.5
yt-dlp>=2024.00.00
python-dotenv>=1.0.0
aiofiles>=23.2.1

5. Instructions for AI Agent

    Async Runtime: Wrap all yt-dlp blocking operations in loop.run_in_executor().

    File Limit: Enforce a hard cap of 2000 MB before and after downloading.

    Audio/Video Extraction:

        Video: bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best

        Audio: Best audio quality extracted as .mp3 via system ffmpeg.

    Cleanup Guarantee: Force file deletion (os.remove) in finally: blocks.

    Pyrogram Settings: Use native client session with supports_streaming=True on send_video().