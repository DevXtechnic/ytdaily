# YTDaily

A powerful, interactive YouTube Feed Downloader that monitors channels and playlists for new content and downloads them automatically in 720p MP4 format with subtitles, SponsorBlock, and parallel download support.

## Features

- 🚀 **Smart Channel Tracking**: Automatically detects and downloads new videos from your followed channels.
- 📋 **Playlist Support**: Monitor and download entire playlists with resume support.
- ⚡ **Parallel Downloads**: Download multiple videos simultaneously to maximize your bandwidth.
- 🎭 **Privacy & Cleanup**: Downloads are kept private, and old videos can be automatically cleaned up.
- 🎙️ **Audio/Podcast Mode**: Option to download as high-quality MP3 (320kbps).
- 🎬 **Quality Control**: Configurable maximum resolution (default 720p).
- 🧹 **SponsorBlock**: Automatically remove sponsors, intros, outros, and more.
- 📊 **Statistics**: Detailed TUI for tracking your download history and storage usage.

## Requirements

- Python 3.7+
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [ffmpeg](https://ffmpeg.org/)
- python-rich library

## Installation

1. Install system dependencies:
   ```bash
   # Arch Linux
   sudo pacman -S yt-dlp ffmpeg
   
   # Ubuntu/Debian
   sudo apt install yt-dlp ffmpeg
   ```

2. Install python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Run the script in interactive mode to manage your channels and settings:

```bash
python3 YT_daily2.py --interactive
```

### Options:
- **Run automatic download**: Checks all monitored channels for new videos.
- **Manage channels**: Add or remove YouTube channels by their handle or ID.
- **Manage playlists**: Track large playlists for new updates.
- **Download single video/audio**: One-off downloads via URL.
- **Settings**: Configure parallel downloads, quality, and more.

## Configuration

Settings and history are stored in `~/.YT_log/`. The script automatically handles directory renaming with duration tags for easier media management.

## License

MIT
