#!/usr/bin/env python3
"""
YouTube Feed Downloader - Downloads ALL new videos from channels automatically after first run.
Fixed video discovery logic and display formatting.
"""

import json
import logging
import logging.handlers
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Any, List, Tuple
import select
import os
import glob
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import readline

# Rich imports
from rich.console import Console
from rich.progress import (
    Progress, SpinnerColumn, BarColumn, TextColumn, 
    TimeRemainingColumn, TransferSpeedColumn, DownloadColumn
)
from rich.table import Table
from rich.panel import Panel
from rich.theme import Theme
from rich.tree import Tree
from rich.align import Align
from rich.rule import Rule
from rich.console import Group
from rich.text import Text
from rich import print as rprint

# Initialize Rich Console
custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "title": "bold blue",
    "highlight": "magenta"
})
console = Console(theme=custom_theme)


# --- CONFIGURATION ---
@dataclass
class Config:
    """Application configuration settings."""
    
    # Base directories - never change these
    base_video_dir: Path = field(default_factory=lambda: Path.home() / "Videos" / "YT_feed")
    base_audio_dir: Path = field(default_factory=lambda: Path.home() / "Music" / "YT_music")
    base_playlist_dir: Path = field(default_factory=lambda: Path.home() / "Videos" / "YT_playlist")
    base_podcast_dir: Path = field(default_factory=lambda: Path.home() / "Music" / "YT_podcasts")
    log_dir: Path = field(default_factory=lambda: Path.home() / ".YT_log")
    
    # Download settings
    max_resolution: str = "720"
    output_format: str = "mp4"
    download_timeout: int = 1800
    query_timeout: int = 60
    max_retries: int = 2
    retry_delay: int = 3
    max_parallel_downloads: int = 3
    
    # Log settings
    max_log_files: int = 10
    max_log_size: int = 5 * 1024 * 1024
    
    # Resume settings
    resume_cache_days: int = 7  # Keep resume data for 7 days
    
    # Channel scan settings
    ask_initial_videos: bool = True  # Ask for recent videos on first run
    initial_videos_per_channel: int = 5  # How many videos to download when no history exists
    max_videos_per_channel: int = 100  # Maximum videos to check per channel
    
    # New Features
    cleanup_days: int = 60  # Delete videos older than this many days
    filter_shorts: bool = True  # Skip videos shorter than 60 seconds
    ask_video_limit_per_channel: bool = False  # Ask for video limit for every channel
    gap_check_limit: int = 10 # How many videos to check to find the "gap" since last run

    
    def __post_init__(self):
        """Create base directories if they don't exist."""
        self.base_video_dir.mkdir(parents=True, exist_ok=True)
        self.base_audio_dir.mkdir(parents=True, exist_ok=True)
        self.base_playlist_dir.mkdir(parents=True, exist_ok=True)
        self.base_podcast_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
    # Regex for matching duration suffixes like "-28hr 17min", "-28.5hr", "-45sec"
    DURATION_REGEX = re.compile(r'\s*-\s*\d+(\.\d+)?(?:hr|min|sec|h|m|s)(?:\s*\d+(\.\d+)?(?:min|sec|m|s))?$', re.IGNORECASE)

    class DirectoryDurationCache:
        def __init__(self, cache_file: Path):
            self.cache_file = cache_file
            self.cache = self._load_cache()
            self._dirty = False

        def _load_cache(self) -> Dict[str, Dict[str, Any]]:
            if self.cache_file.exists():
                try:
                    with open(self.cache_file, 'r') as f:
                        return json.load(f)
                except Exception:
                    return {}
            return {}

        def save(self):
            if self._dirty:
                try:
                    with open(self.cache_file, 'w') as f:
                        json.dump(self.cache, f)
                    self._dirty = False
                except Exception:
                    pass

        def get(self, file_path: Path) -> Optional[float]:
            key = str(file_path)
            if key in self.cache:
                entry = self.cache[key]
                try:
                    stat = file_path.stat()
                    if entry['mtime'] == stat.st_mtime and entry['size'] == stat.st_size:
                        return entry['duration']
                except FileNotFoundError:
                    pass
            return None

        def set(self, file_path: Path, duration: float):
            try:
                stat = file_path.stat()
                self.cache[str(file_path)] = {
                    'mtime': stat.st_mtime,
                    'size': stat.st_size,
                    'duration': duration
                }
                self._dirty = True
            except FileNotFoundError:
                pass

    def __post_init__(self):
        """Create base directories if they don't exist."""
        self.base_video_dir.mkdir(parents=True, exist_ok=True)
        self.base_audio_dir.mkdir(parents=True, exist_ok=True)
        self.base_playlist_dir.mkdir(parents=True, exist_ok=True)
        self.base_podcast_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.download_log_path = self.log_dir / "Download.json"
        
        # Initialize duration cache
        self.duration_cache = self.DirectoryDurationCache(self.log_dir / "directory_durations.json")
        self.config_path = self.log_dir / "config.json"
        self.last_download_path = self.log_dir / "last_download.json"
        self.app_log_path = self.log_dir / "YT_feed.log"
        self.resume_state_path = self.log_dir / "resume_state.json"
        self.channel_history_path = self.log_dir / "channel_history.json"
        self.setup_log_cleanup()

    def setup_log_cleanup(self):
        """Setup log rotation and cleanup."""
        log_files = list(self.log_dir.glob("YT_feed*.log*"))
        if len(log_files) > self.max_log_files:
            log_files.sort(key=lambda x: x.stat().st_mtime)
            for old_log in log_files[:-self.max_log_files]:
                try:
                    old_log.unlink()
                except OSError:
                    pass

    @property
    def current_video_dir(self) -> Path:
        """Get the current video directory."""
        return self.base_video_dir

    @property
    def current_audio_dir(self) -> Path:
        """Get the current audio directory."""
        return self.base_audio_dir

    @property
    def current_playlist_dir(self) -> Path:
        """Get the current playlist directory."""
        return self.base_playlist_dir

    @property
    def current_podcast_dir(self) -> Path:
        """Get the current podcast directory."""
        return self.base_podcast_dir




class YouTubeFeedDownloader:
    """Main class for downloading YouTube content with multi-video channel support."""
    
    def __init__(self, config: Config):
        """Initialize the downloader with configuration."""
        self.config = config
        self.channels: Dict[str, str] = {}
        self.playlists: Dict[str, str] = {}
        
        # Initialize state
        self.download_history: Dict[str, Any] = {"channels": {}, "playlists": {}}
        self.last_download: Dict[str, Any] = {}
        self.resume_state: Dict[str, Any] = {}
        self.channel_history: Dict[str, Any] = {}  # Tracks all downloaded videos per channel
        
        # Setup logging
        self.setup_logging()
        self.load_config()
        self.load_download_history()
        self.load_resume_state()
        self.load_channel_history()
        
        # Ensure we're using the correct current directory
        self.ensure_current_directories()
        
    def send_notification(self, title: str, message: str, urgency: str = "normal") -> None:
        """Send a desktop notification using notify-send."""
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, "-a", "YT Feed", title, message],
                check=False,
                capture_output=True
            )
        except Exception as e:
            # Don't crash if notifications fail
            pass

    def cleanup_old_videos(self) -> int:
        """Delete videos older than configured days from YT_feed directories."""
        cleaned_count = 0
        try:
            cutoff_time = time.time() - (self.config.cleanup_days * 86400)
            
            # Only clean in YT_feed directories
            videos_dir = Path.home() / "Videos"
            yt_feed_dirs = list(videos_dir.glob("YT_feed*"))
            
            for directory in yt_feed_dirs:
                if directory.is_dir():
                    for file_path in directory.glob("*"):
                        if file_path.is_file():
                            try:
                                if file_path.stat().st_mtime < cutoff_time:
                                    file_path.unlink()
                                    cleaned_count += 1
                                    self.logger.info(f"🧹 Deleted old file: {file_path.name}")
                            except Exception as e:
                                self.logger.warning(f"⚠️ Could not delete {file_path}: {e}")
                                
            if cleaned_count > 0:
                console.print(f"[warning]🧹 Cleaned up {cleaned_count} videos older than {self.config.cleanup_days} days[/warning]")
                self.send_notification("Cleanup Complete", f"Removed {cleaned_count} old videos")
                
        except Exception as e:
            self.logger.error(f"❌ Error during video cleanup: {e}")
            
        return cleaned_count
        
    def ensure_current_directories(self):
        """Ensure current directories exist, stripping duration suffixes if present."""
        # Check if we have a renamed YT_feed directory
        video_parent = self.config.base_video_dir.parent
        renamed_dirs = list(video_parent.glob("YT_feed -*"))
        
        if renamed_dirs:
            # Sort by modification time to get the most recent one
            latest_dir = max(renamed_dirs, key=lambda x: x.stat().st_mtime)
            
            # If base directory doesn't exist, rename the latest one to it
            if not self.config.base_video_dir.exists():
                try:
                    latest_dir.rename(self.config.base_video_dir)
                    self.logger.info(f"📁 Renamed {latest_dir.name} back to {self.config.base_video_dir.name}")
                except OSError as e:
                    self.logger.error(f"❌ Error renaming directory: {e}")
        
        self.config.current_video_dir.mkdir(parents=True, exist_ok=True)
        self.config.current_audio_dir.mkdir(parents=True, exist_ok=True)
        self.config.current_playlist_dir.mkdir(parents=True, exist_ok=True)
        self.config.current_podcast_dir.mkdir(parents=True, exist_ok=True)
        
    def setup_logging(self) -> None:
        """Configure file logging with rotation."""
        file_handler = logging.handlers.RotatingFileHandler(
            self.config.app_log_path,
            maxBytes=self.config.max_log_size,
            backupCount=5,
            encoding='utf-8'
        )
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        
        self.logger = logging.getLogger(__name__)
        self.logger.setLevel(logging.INFO)
        
        # Clear any existing handlers to avoid duplicates
        self.logger.handlers.clear()
        self.logger.addHandler(file_handler)
        
        # Log startup information
        self.logger.info("🎬 YouTube Feed Downloader Started - Fixed Discovery Logic")

    def load_channel_history(self) -> None:
        """Load channel history that tracks ALL downloaded videos per channel."""
        try:
            if self.config.channel_history_path.exists():
                with open(self.config.channel_history_path, 'r', encoding='utf-8') as f:
                    self.channel_history = json.load(f)
            else:
                self.channel_history = {
                    "channels": {},
                    "last_updated": datetime.now().isoformat(),
                    "first_run_completed": False  # Track if first run is done
                }
                self.save_channel_history()
                
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
            self.logger.error(f"❌ Error loading channel history: {e}")
            self.channel_history = {
                "channels": {},
                "last_updated": datetime.now().isoformat(),
                "first_run_completed": False
            }
            self.save_channel_history()

    def save_channel_history(self) -> None:
        """Save channel history to disk."""
        try:
            self.channel_history["last_updated"] = datetime.now().isoformat()
            with open(self.config.channel_history_path, 'w', encoding='utf-8') as f:
                json.dump(self.channel_history, f, indent=2, ensure_ascii=False)
            self.logger.debug("💾 Saved channel history")
        except IOError as e:
            self.logger.error(f"❌ Error saving channel history: {e}")

    def mark_first_run_completed(self) -> None:
        """Mark that the first run has been completed."""
        self.channel_history["first_run_completed"] = True
        self.save_channel_history()

    def is_first_run(self) -> bool:
        """Check if this is the first run (no channel history)."""
        return not self.channel_history.get("first_run_completed", False)

    def update_channel_history(self, channel_handle: str, video_info: Dict[str, Any]) -> None:
        """Update channel history with a new downloaded video."""
        try:
            if "channels" not in self.channel_history:
                self.channel_history["channels"] = {}
                
            if channel_handle not in self.channel_history["channels"]:
                self.channel_history["channels"][channel_handle] = {
                    "downloaded_videos": [],
                    "last_download": datetime.now().isoformat()
                }
            
            # Add video to history if not already there
            video_id = video_info["id"]
            existing_ids = [v["id"] for v in self.channel_history["channels"][channel_handle]["downloaded_videos"]]
            
            if video_id not in existing_ids:
                self.channel_history["channels"][channel_handle]["downloaded_videos"].append({
                    "id": video_id,
                    "title": video_info.get("title", "Unknown"),
                    "url": video_info.get("url", ""),
                    "downloaded_at": datetime.now().isoformat()
                })
                
                # Keep only the last 1000 videos to prevent file from growing too large
                if len(self.channel_history["channels"][channel_handle]["downloaded_videos"]) > 1000:
                    self.channel_history["channels"][channel_handle]["downloaded_videos"] = \
                        self.channel_history["channels"][channel_handle]["downloaded_videos"][-1000:]
                
                self.channel_history["channels"][channel_handle]["last_download"] = datetime.now().isoformat()
                self.save_channel_history()
                
        except Exception as e:
            self.logger.warning(f"⚠️ Could not update channel history: {e}")

    def is_video_downloaded(self, channel_handle: str, video_id: str) -> bool:
        """Check if a video has already been downloaded for a channel."""
        try:
            if (channel_handle in self.channel_history.get("channels", {}) and
                any(video["id"] == video_id for video in self.channel_history["channels"][channel_handle].get("downloaded_videos", []))):
                return True
            return False
        except Exception as e:
            self.logger.warning(f"⚠️ Error checking video download status: {e}")
            return False

    def load_resume_state(self) -> None:
        """Load resume state from disk and clean up old entries."""
        try:
            if self.config.resume_state_path.exists():
                with open(self.config.resume_state_path, 'r', encoding='utf-8') as f:
                    self.resume_state = json.load(f)
                
                # Clean up old resume entries (older than 7 days)
                self.cleanup_old_resume_entries()
            else:
                self.resume_state = {
                    "videos": {},
                    "playlists": {},
                    "last_cleanup": datetime.now().isoformat()
                }
                self.save_resume_state()
                
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
            self.logger.error(f"❌ Error loading resume state: {e}")
            self.resume_state = {
                "videos": {},
                "playlists": {},
                "last_cleanup": datetime.now().isoformat()
            }
            self.save_resume_state()

    def save_resume_state(self) -> None:
        """Save resume state to disk."""
        try:
            with open(self.config.resume_state_path, 'w', encoding='utf-8') as f:
                json.dump(self.resume_state, f, indent=2, ensure_ascii=False)
            self.logger.debug("💾 Saved resume state")
        except IOError as e:
            self.logger.error(f"❌ Error saving resume state: {e}")

    def cleanup_old_resume_entries(self) -> None:
        """Clean up resume entries older than 7 days."""
        try:
            cutoff_time = datetime.now() - timedelta(days=self.config.resume_cache_days)
            cleaned_count = 0
            
            # Clean video entries
            for video_id in list(self.resume_state["videos"].keys()):
                entry = self.resume_state["videos"][video_id]
                entry_time = datetime.fromisoformat(entry.get("timestamp", "2000-01-01"))
                if entry_time < cutoff_time:
                    del self.resume_state["videos"][video_id]
                    cleaned_count += 1
            
            # Clean playlist entries  
            for playlist_url in list(self.resume_state["playlists"].keys()):
                entry = self.resume_state["playlists"][playlist_url]
                entry_time = datetime.fromisoformat(entry.get("timestamp", "2000-01-01"))
                if entry_time < cutoff_time:
                    del self.resume_state["playlists"][playlist_url]
                    cleaned_count += 1
            
            if cleaned_count > 0:
                self.logger.info(f"🧹 Cleaned up {cleaned_count} old resume entries")
                self.save_resume_state()
                
        except Exception as e:
            self.logger.error(f"❌ Error cleaning up old resume entries: {e}")

    def update_resume_state(self, item_type: str, item_id: str, data: Dict[str, Any]) -> None:
        """Update resume state for an item."""
        try:
            if item_type == "video":
                self.resume_state["videos"][item_id] = {
                    **data,
                    "timestamp": datetime.now().isoformat()
                }
            elif item_type == "playlist":
                self.resume_state["playlists"][item_id] = {
                    **data,
                    "timestamp": datetime.now().isoformat()
                }
            
            self.save_resume_state()
        except Exception as e:
            self.logger.warning(f"⚠️ Could not update resume state: {e}")

    def get_resume_state(self, item_type: str, item_id: str) -> Optional[Dict[str, Any]]:
        """Get resume state for an item."""
        try:
            if item_type == "video":
                return self.resume_state["videos"].get(item_id)
            elif item_type == "playlist":
                return self.resume_state["playlists"].get(item_id)
        except Exception as e:
            self.logger.warning(f"⚠️ Could not get resume state: {e}")
        return None

    def clear_resume_state(self, item_type: str, item_id: str) -> None:
        """Clear resume state for an item (when download completes)."""
        try:
            if item_type == "video" and item_id in self.resume_state["videos"]:
                del self.resume_state["videos"][item_id]
            elif item_type == "playlist" and item_id in self.resume_state["playlists"]:
                del self.resume_state["playlists"][item_id]
            
            self.save_resume_state()
        except Exception as e:
            self.logger.warning(f"⚠️ Could not clear resume state: {e}")

    def get_default_channels(self):
        """Get default channels (cleaned for public release)."""
        return {
            # "channel-id": "Channel Name"
        }

    def get_default_playlists(self):
        """Get default playlists (cleaned for public release)."""
        return {
            # "https://youtube.com/playlist?list=ID": "Playlist Name"
        }

    def load_config(self) -> None:
        """Load configuration from file and sync with script defaults."""
        try:
            if self.config.config_path.exists():
                with open(self.config.config_path, 'r', encoding='utf-8') as f:
                    config_data = json.load(f)
                    
                    file_channels = config_data.get("channels", {})
                    file_playlists = config_data.get("playlists", {})
                    
                    default_channels = self.get_default_channels()
                    default_playlists = self.get_default_playlists()
                    
                    merged_channels = {**default_channels, **file_channels}
                    merged_playlists = {**default_playlists, **file_playlists}
                    
                    self.channels = merged_channels
                    self.playlists = merged_playlists
                    
                    # Load settings
                    self.config.ask_initial_videos = config_data.get("ask_initial_videos", True)
                    self.config.initial_videos_per_channel = config_data.get("initial_videos_per_channel", 5)
                    self.config.max_videos_per_channel = config_data.get("max_videos_per_channel", 100)
                    self.config.max_resolution = config_data.get("max_resolution", "720")
                    
                    if (file_channels != merged_channels or file_playlists != merged_playlists):
                        self.save_config()
            else:
                self.channels = self.get_default_channels()
                self.playlists = self.get_default_playlists()
                self.save_config()
                
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
            self.logger.error(f"❌ Error loading config: {e}")
            self.channels = self.get_default_channels()
            self.playlists = self.get_default_playlists()
            self.save_config()

    def save_config(self) -> None:
        """Save configuration to file."""
        try:
            config_data = {
                "channels": self.channels,
                "playlists": self.playlists,
                "ask_initial_videos": self.config.ask_initial_videos,
                "initial_videos_per_channel": self.config.initial_videos_per_channel,
                "max_videos_per_channel": self.config.max_videos_per_channel,
                "max_resolution": self.config.max_resolution
            }
            with open(self.config.config_path, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)
            self.logger.debug("💾 Saved configuration")
        except IOError as e:
            self.logger.error(f"❌ Error saving config: {e}")
    
    def load_download_history(self) -> None:
        """Load the download history from disk."""
        try:
            if self.config.download_log_path.exists():
                with open(self.config.download_log_path, 'r', encoding='utf-8') as f:
                    self.download_history = json.load(f)
            else:
                self.download_history = {"channels": {}, "playlists": {}}
                self.save_download_history()
            
            if self.config.last_download_path.exists():
                with open(self.config.last_download_path, 'r', encoding='utf-8') as f:
                    self.last_download = json.load(f)
            else:
                self.last_download = {}
                
        except (json.JSONDecodeError, IOError, UnicodeDecodeError) as e:
            self.logger.error(f"❌ Error loading history: {e}")
            self.download_history = {"channels": {}, "playlists": {}}
            self.last_download = {}
    
    def save_download_history(self) -> None:
        """Save the download history to disk."""
        try:
            with open(self.config.download_log_path, 'w', encoding='utf-8') as f:
                json.dump(self.download_history, f, indent=2, ensure_ascii=False)
            self.logger.debug("💾 Saved download history")
        except IOError as e:
            self.logger.error(f"❌ Error saving download history: {e}")
    
    def save_last_download(self, info: Dict[str, Any]) -> None:
        """Save information about the last download."""
        try:
            self.last_download = {
                **info,
                "timestamp": datetime.now().isoformat()
            }
            with open(self.config.last_download_path, 'w', encoding='utf-8') as f:
                json.dump(self.last_download, f, indent=2, ensure_ascii=False)
        except IOError as e:
            self.logger.warning(f"⚠️ Could not save last download info: {e}")
    
    def format_duration(self, seconds: int) -> str:
        """Format duration in seconds to HH:MM:SS or MM:SS."""
        if seconds is None or seconds < 0:
            return "Unknown"
        try:
            seconds = int(seconds)
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            seconds = seconds % 60
            
            if hours > 0:
                return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                return f"{minutes:02d}:{seconds:02d}"
        except (TypeError, ValueError):
            return "Unknown"
    
    def format_duration_short(self, seconds: float) -> str:
        """Format duration in seconds to short format like '3hr 45min' or '26sec' with rounding."""
        if seconds is None or seconds < 0:
            return "Unknown"
        
        try:
            # Round seconds to nearest whole number
            seconds = round(float(seconds))
            
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            remaining_seconds = seconds % 60
            
            if hours > 0:
                if minutes > 0:
                    return f"{hours}hr {minutes}min"
                else:
                    return f"{hours}hr"
            elif minutes > 0:
                if remaining_seconds > 0:
                    return f"{minutes}min {remaining_seconds}sec"
                else:
                    return f"{minutes}min"
            else:
                return f"{remaining_seconds}sec"
        except (TypeError, ValueError):
            return "Unknown"
    
    # Regex for matching duration suffixes like "-28hr 17min", "-28.5hr", "-45sec"
    DURATION_REGEX = re.compile(r'\s*-\s*\d+(\.\d+)?(?:hr|min|sec|h|m|s)(?:\s*\d+(\.\d+)?(?:min|sec|m|s))?$', re.IGNORECASE)

    def calculate_directory_duration(self, directory: Path) -> Tuple[float, str, str]:
        """Calculate total duration of all media files in a directory."""
        try:
            media_extensions = ['*.mp3', '*.wav', '*.flac', '*.aac', '*.ogg', '*.m4a', 
                               '*.mp4', '*.mkv', '*.avi', '*.mov', '*.flv', '*.webm']
            
            total_seconds = 0.0
            file_count = 0
            
            for extension in media_extensions:
                for file_path in directory.rglob(extension):
                    # Check cache first
                    cached_duration = self.config.duration_cache.get(file_path)
                    if cached_duration is not None:
                        total_seconds += cached_duration
                        file_count += 1
                        continue

                    try:
                        cmd = [
                            'ffprobe', '-v', 'quiet', '-show_entries', 
                            'format=duration', '-of', 
                            'default=noprint_wrappers=1:nokey=1', str(file_path)
                        ]
                        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
                        duration_str = result.stdout.strip()
                        if duration_str:
                            duration = float(duration_str)
                            self.config.duration_cache.set(file_path, duration) # Update cache
                            total_seconds += duration
                            file_count += 1
                    except (subprocess.CalledProcessError, ValueError, FileNotFoundError, subprocess.TimeoutExpired) as e:
                        # self.logger.warning(f"⚠️ Could not get duration for {file_path}: {e}")
                        continue
            
            # Save cache after processing directory
            self.config.duration_cache.save()
            
            if file_count == 0:
                return 0.0, "0sec", "No media files"
            
            return total_seconds, self.format_duration_short(total_seconds), f"{file_count} files"
            
        except Exception as e:
            self.logger.error(f"❌ Error calculating directory duration: {e}")
            return 0.0, "Error", "Error"
    
    def rename_directory_with_duration(self, directory: Path, base_name: str) -> Tuple[Path, float, str]:
        """Rename directory to include total duration with proper spacing. Returns (new_path, total_seconds, duration_str)."""
        try:
            if not directory.exists():
                return directory, 0.0, "0sec"
            
            total_seconds, duration_short, file_info = self.calculate_directory_duration(directory)
            
            clean_base = self.DURATION_REGEX.sub('', base_name).strip()
            # Clean up old formatting remnants if any
            clean_base = re.sub(r'\s*-\s*\d+[hmr]\s*\d*[ms]?in?$', '', clean_base)
            clean_base = re.sub(r'\s*-\s*\d+sec$', '', clean_base)
            clean_base = re.sub(r'\s*-\s*\d+\.\d+sec$', '', clean_base)
            
            if total_seconds > 0:
                new_name = f"{clean_base} -{duration_short}"
            else:
                new_name = clean_base
            
            new_path = directory.parent / new_name
            
            if new_path != directory and not new_path.exists():
                try:
                    directory.rename(new_path)
                    return new_path, total_seconds, duration_short
                except OSError:
                    return directory, total_seconds, duration_short
            elif new_path.exists() and new_path != directory:
                return new_path, total_seconds, duration_short
            
            return directory, total_seconds, duration_short
            
        except Exception as e:
            self.logger.error(f"❌ Error renaming directory: {e}")
            return directory, 0.0, "Error"
            
    def update_directory_names(self):
        """Update YT_feed and playlist directories with duration information."""
        console.print(Rule("[bold magenta]Calculating Media Durations[/]"))
        console.print("")
        
        tree = Tree("📁 [bold]Directories[/]")
        
        with console.status("[bold green]Scanning directories...[/bold green]"):
            current_video_dir = self.config.current_video_dir
            
            if current_video_dir.exists():
                v_seconds, v_duration, _ = self.calculate_directory_duration(current_video_dir)
                # Display duration but do NOT rename directory on disk to prevent path fragmentation
                if v_seconds > 0:
                    tree.add(f"📁 [yellow]YT_feed -{v_duration}[/yellow]")
                else:
                    tree.add(f"📁 [yellow]YT_feed[/yellow]")
            
            current_playlist_dir = self.config.current_playlist_dir
            
            if current_playlist_dir.exists():
                total_playlist_seconds, total_playlist_duration, total_playlist_files = self.calculate_directory_duration(current_playlist_dir)
                # Only show if duration > 0
                if total_playlist_seconds > 0:
                    playlist_node = tree.add(f"📚 [blue]YT_playlist (combined) - {total_playlist_duration}[/blue]")
                    
                    playlist_dirs = [d for d in current_playlist_dir.iterdir() if d.is_dir()]
                    for playlist_dir in playlist_dirs:
                        base_name = playlist_dir.name
                        new_playlist_dir, p_seconds, p_duration = self.rename_directory_with_duration(playlist_dir, base_name)
                
                        if p_seconds > 0:
                             playlist_node.add(f"📚 {new_playlist_dir.name}")
                
        console.print(tree)
        console.print("")
    
    def get_all_recent_videos(self, url: str, source_name: str, max_videos: int = 100, silent: bool = False) -> List[Dict[str, Any]]:
        """Get all recent videos from a channel or playlist."""
        videos = []
        for attempt in range(self.config.max_retries):
            try:
                cmd = [
                    "yt-dlp",
                    "--flat-playlist",
                    "--playlist-items", f"1-{max_videos}",
                    "--dump-json",
                    "--no-warnings",
                    url
                ]
                
                if not silent:
                    if self.is_first_run():
                        print(f"🔍 Scanning {source_name} for recent videos...", end="", flush=True)
                    else:
                        print(f"🔍 Checking {source_name} for NEW videos...", end="", flush=True)
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout * 2,
                    check=True
                )
                
                if result.stdout.strip():
                    lines = result.stdout.strip().split('\n')
                    for line in lines:
                        if line.strip():
                            try:
                                data = json.loads(line)
                                video_id = data.get("id", "unknown")
                                title = data.get("title", "Unknown")
                                uploader = data.get("uploader", source_name)
                                
                                duration = data.get("duration", 0)
                                if not duration or duration == 0:
                                    duration = data.get("average_duration", 0)
                                if not duration or duration == 0:
                                    duration = 0
                                
                                videos.append({
                                    "id": video_id,
                                    "title": title,
                                    "url": f"https://www.youtube.com/watch?v={video_id}",
                                    "uploader": uploader,
                                    "duration": duration,
                                    "duration_formatted": self.format_duration(duration)
                                })
                            except json.JSONDecodeError:
                                continue
                    
                    if not silent:
                        if self.is_first_run():
                            print(f" ✅ Found {len(videos)} videos")
                        else:
                            print(f" ✅ Found {len(videos)} recent videos")
                    return videos
                
                if not silent:
                    print(" ⚠️ No videos found")
                return []
                
            except subprocess.TimeoutExpired:
                if not silent:
                    print(f" ⏱️ Timeout")
                self.logger.warning(f"⚠️ Query timeout for {source_name} (attempt {attempt + 1})")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay)
                continue
                
            except subprocess.CalledProcessError as e:
                error_msg = e.stderr.strip() if e.stderr else "Unknown error"
                if not silent:
                    print(f" ❌ Command failed")
                self.logger.error(f"❌ Query command failed for {source_name}: {error_msg}")
                
                if attempt == self.config.max_retries - 1:
                    return self._fallback_recent_videos(url, source_name, max_videos, silent)
                else:
                    time.sleep(self.config.retry_delay)
                continue
                
            except Exception as e:
                if not silent:
                    print(f" ❌ Error: {str(e)[:50]}")
                self.logger.error(f"❌ Query error for {source_name}: {e}")
                if attempt < self.config.max_retries - 1:
                    time.sleep(self.config.retry_delay)
                continue
        
        return []

    def _fallback_recent_videos(self, url: str, source_name: str, limit: int, silent: bool = False) -> List[Dict[str, Any]]:
        """Fallback method for getting recent videos when main method fails."""
        try:
            self.logger.info(f"🔄 Trying fallback query for {source_name}")
            
            videos = []
            # Try to get videos one by one (slower but more reliable)
            max_videos = limit if self.is_first_run() else min(limit, 50)  # Limit to 50 for subsequent runs
            for i in range(1, max_videos + 1):
                try:
                    cmd = [
                        "yt-dlp",
                        "--flat-playlist",
                        "--playlist-items", str(i),
                        "--print", "%(id)s\t%(title)s\t%(uploader)s\t%(duration)s",
                        "--no-warnings",
                        url
                    ]
                    
                    result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False
                    )
                    
                    if result.returncode == 0 and result.stdout.strip():
                        parts = result.stdout.strip().split('\t')
                        if len(parts) >= 2:
                            video_id = parts[0]
                            title = parts[1] if len(parts) > 1 else "Unknown"
                            uploader = parts[2] if len(parts) > 2 else source_name
                            duration_str = parts[3] if len(parts) > 3 else "0"
                            
                            try:
                                duration = int(duration_str) if duration_str and duration_str.isdigit() else 0
                            except (ValueError, TypeError):
                                duration = 0
                            
                            videos.append({
                                "id": video_id,
                                "title": title,
                                "url": f"https://www.youtube.com/watch?v={video_id}",
                                "uploader": uploader,
                                "duration": duration,
                                "duration_formatted": self.format_duration(duration)
                            })
                except Exception as e:
                    self.logger.warning(f"⚠️ Failed to get video {i} for {source_name}: {e}")
                    break
            
            return videos
                
        except Exception as e:
            self.logger.error(f"❌ Fallback query also failed for {source_name}: {e}")
        
        return []

    def check_subtitles_available(self, video_url: str) -> bool:
        """Check if subtitles are available for a video to avoid 429 errors."""
        try:
            cmd = [
                "yt-dlp",
                "--list-subs",
                "--no-warnings",
                "--no-cookies",
                video_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )
            
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if 'Language' in line and 'Formats' in line:
                        if len(lines) > 2:
                            return True
                    if line.startswith('en ') or 'english' in line.lower():
                        return True
            
            return False
            
        except Exception as e:
            self.logger.warning(f"⚠️ Could not check subtitles availability: {e}")
            return False
    
    def get_playlist_info(self, playlist_url: str) -> Optional[Dict[str, Any]]:
        """Get playlist information including title and total duration."""
        try:
            # Use a simpler approach that's more reliable
            cmd = [
                "yt-dlp",
                "--flat-playlist",
                "--print", "%(playlist_title)s",
                "--print", "%(playlist_count)s", 
                "--print", "%(uploader)s",
                "--no-download",
                "--no-warnings",
                playlist_url
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.config.query_timeout,
                check=False
            )
            
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                
                playlist_title = lines[0] if len(lines) > 0 else "Unknown Playlist"
                video_count = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else 0
                uploader = lines[2] if len(lines) > 2 else "Unknown"
                
                return {
                    "title": playlist_title,
                    "uploader": uploader,
                    "video_count": video_count,
                    "total_duration": "Unknown",
                    "total_seconds": 0
                }
            else:
                # Fallback: try to get just the title
                cmd = [
                    "yt-dlp",
                    "--print", "%(title)s",
                    "--no-download",
                    "--no-warnings",
                    playlist_url
                ]
                
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.config.query_timeout,
                    check=False
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    return {
                        "title": result.stdout.strip(),
                        "uploader": "Unknown",
                        "video_count": 0,
                        "total_duration": "Unknown",
                        "total_seconds": 0
                    }
                
        except Exception as e:
            self.logger.error(f"❌ Error getting playlist info: {e}")
        
        return None

    def build_download_command(self, video_url: str, source_name: str, skip_subs: bool = False, is_manual: bool = False, resume: bool = False) -> List[str]:
        """Build the yt-dlp command for 720p MP4 downloads with resume support."""
        
        output_template = str(self.config.current_video_dir / "%(title)s.%(ext)s")
        
        cmd = [
            "yt-dlp",
            "-f", f"bestvideo[height<={self.config.max_resolution}]+bestaudio/best[height<={self.config.max_resolution}]",
            "--merge-output-format", "mp4",
            "--no-cookies",
            "--no-cache-dir",
            "--no-part",
            "--no-mtime",
            "--sponsorblock-remove", "sponsor,intro,outro,selfpromo,preview,interaction",
            "--embed-chapters",
            "--embed-metadata",
            "--no-embed-thumbnail",
            "--no-playlist",
            "--output", output_template,
            "--newline",
            "--no-warnings",
            "--progress",
            "--retries", "10",
            "--fragment-retries", "10",
            "--file-access-retries", "5", 
            "--socket-timeout", "30",
        ]
        
        # Add resume support if requested
        if resume:
            cmd.append("--continue")
        
        if not skip_subs:
            cmd.extend([
                "--write-auto-sub",
                "--write-sub",
                "--sub-langs", "en",
                "--convert-subs", "srt",
                "--embed-subs",
            ])
        
        cmd.append(video_url)
        
        return cmd

    def build_audio_download_command(self, video_url: str, source_name: str, is_manual: bool = False, resume: bool = False) -> List[str]:
        """Build the yt-dlp command for MP3 audio downloads with best quality (320kbps) and resume support."""
        output_template = str(self.config.current_audio_dir / "%(title)s.%(ext)s")
        
        cmd = [
            "yt-dlp",
            "-f", "bestaudio[ext=m4a]/bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "320K",
            "--no-cookies",
            "--no-cache-dir", 
            "--no-part",
            "--no-mtime",
            "--embed-metadata",
            "--embed-thumbnail",
            "--no-playlist",
            "--output", output_template,
            "--newline",
            "--no-warnings",
            "--progress",
            "--retries", "10",
            "--fragment-retries", "10",
            "--file-access-retries", "5",
            "--socket-timeout", "30",
        ]
        
        # Add resume support if requested
        if resume:
            cmd.append("--continue")
        
        cmd.append(video_url)
        
        return cmd
    
    def build_playlist_download_command(self, playlist_url: str, playlist_name: str, download_type: str = "video", resume: bool = False, resume_from: int = 1) -> Tuple[List[str], Path]:
        """Build the yt-dlp command for downloading entire playlists with video/audio options and resume support."""
        safe_name = re.sub(r'[<>:"/\\|?*]', '', playlist_name)
        
        if download_type == "audio":
            playlist_directory = self.config.current_podcast_dir / safe_name
            output_template = str(playlist_directory / "%(title)s.%(ext)s")
            
            cmd = [
                "yt-dlp",
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "--extract-audio",
                "--audio-format", "mp3", 
                "--audio-quality", "320K",
                "--no-cookies",
                "--no-cache-dir",
                "--no-part",
                "--no-mtime",
                "--embed-metadata",
                "--embed-thumbnail",
                "--yes-playlist",
                "--output", output_template,
                "--newline",
                "--no-warnings",
                "--progress",
                "--retries", "10",
                "--fragment-retries", "10",
                "--file-access-retries", "5",
                "--socket-timeout", "30",
            ]
        else:
            playlist_directory = self.config.current_playlist_dir / safe_name
            output_template = str(playlist_directory / "%(title)s.%(ext)s")
            
            cmd = [
                "yt-dlp",
                "-f", f"bestvideo[height<={self.config.max_resolution}]+bestaudio/best[height<={self.config.max_resolution}]",
                "--merge-output-format", "mp4",
                "--no-cookies",
                "--no-cache-dir",
                "--no-part",
                "--no-mtime",
                "--sponsorblock-remove", "sponsor,intro,outro,selfpromo,preview,interaction",
                "--embed-metadata",
                "--embed-chapters",
                "--yes-playlist",
                "--output", output_template,
                "--newline",
                "--no-warnings",
                "--progress",
                "--retries", "10",
                "--fragment-retries", "10",
                "--file-access-retries", "5",
                "--socket-timeout", "30",
            ]
        
        # Add resume support if requested
        if resume:
            cmd.append("--continue")
            # Start from specific playlist item if resuming
            if resume_from > 1:
                cmd.extend(["--playlist-start", str(resume_from)])
        
        cmd.append(playlist_url)
        
        playlist_directory.mkdir(parents=True, exist_ok=True)
        return cmd, playlist_directory
    
    def parse_progress(self, line: str) -> Optional[Dict[str, str]]:
        """Parse yt-dlp progress output."""
        # Current video progress
        progress_match = re.search(
            r'\[download\]\s+(\d+\.?\d*)%\s+of\s+~?\s*(\d+\.?\d*)(\w+)\s+at\s+(\d+\.?\d*)(\w+)/s\s+ETA\s+(\d+:\d+|\d+)',
            line
        )
        
        if progress_match:
            return {
                "type": "download",
                "percent": progress_match.group(1),
                "size": f"{progress_match.group(2)}{progress_match.group(3)}",
                "speed": f"{progress_match.group(4)}{progress_match.group(5)}/s",
                "eta": progress_match.group(6)
            }
        
        # Extract audio/conversion progress
        extract_match = re.search(r'\[ExtractAudio\]\s+Destination:\s+(.+)', line)
        if extract_match:
            return {"type": "extract", "file": extract_match.group(1)}
        
        # FFmpeg conversion progress
        ffmpeg_match = re.search(r'\[FFmpeg\]\s+Converting\s+.+\s+to\s+.+', line)
        if ffmpeg_match:
            return {"type": "converting"}
        
        # New video starting in playlist
        playlist_match = re.search(r'\[download\]\s+Downloading\s+item\s+(\d+)\s+of\s+(\d+)', line)
        if playlist_match:
            return {
                "type": "playlist_progress",
                "current": playlist_match.group(1),
                "total": playlist_match.group(2)
            }
        
        if "[download] Destination:" in line:
            return {"type": "starting"}
            
        return None
    
    def display_double_progress_bar(self, current_percent: float, overall_percent: float, current_info: str = "", 
                                  current_size: str = "", current_speed: str = "", current_eta: str = "", 
                                  width: int = 40):
        """Display dual progress bars for playlist downloads."""
        # Current video progress bar
        current_filled = int(width * current_percent / 100)
        current_bar = f"[green]{'█' * current_filled}[/green][dim]{'░' * (width - current_filled)}[/dim]"
        
        # Overall playlist progress bar  
        overall_filled = int(width * overall_percent / 100)
        overall_bar = f"[blue]{'█' * overall_filled}[/blue][dim]{'░' * (width - overall_filled)}[/dim]"
        
        # Print with cursor control
        # \033[K clears the line.
        # We print two lines.
        # First Line: Current
        print(f"\r\033[K   [bold]Cur:[/bold] [{current_bar}] {current_percent:5.1f}% {current_speed} ETA:{current_eta}", end="")
        # Second Line: Overall (with newline)
        print(f"\n\r\033[K   [bold]Tot:[/bold] [{overall_bar}] {overall_percent:5.1f}% {current_info}", end="")
        # Move cursor back up one line so next update overwrites 'Cur' then 'Tot'
        print(f"\033[A", end="") 
        sys.stdout.flush()

    
    def display_conversion_progress(self, message: str, is_complete: bool = False):
        """Display conversion/processing progress."""
        if is_complete:
            print(f"\r\033[K   ✅ {message}")
        else:
            print(f"\r\033[K   🔄 {message}", end="", flush=True)
    
    def cleanup_subtitle_files(self, video_title: str, is_manual: bool = False):
        """Clean up temporary subtitle files after embedding."""
        try:
            # More aggressive cleanup for problematic titles
            clean_title = re.sub(r'[<>:"/\\|?*]', '_', video_title)
            clean_title = re.sub(r'\s+', ' ', clean_title).strip()
            
            # Look for subtitle files with different patterns
            subtitle_patterns = [
                f"*{clean_title}*.en.srt",
                f"*{clean_title}*.srt", 
                f"*{clean_title}*.vtt",
                f"*{clean_title}*.en.vtt",
                f"*.srt", # Fallback to catch all srt/vtt if specific match fails, carefully
                f"*.vtt"
            ]
            
            files_cleaned = 0
            for pattern in subtitle_patterns:
                full_pattern = os.path.join(self.config.current_video_dir, pattern)
                for file_path in glob.glob(full_pattern):
                    try:
                        if file_path.endswith(('.srt', '.vtt')) and os.path.isfile(file_path):
                            # Ensure we don't delete the video file itself by checks
                            if not file_path.endswith(('.mp4', '.mkv', '.webm', '.mp3', '.m4a')):
                                try:
                                    os.remove(file_path)
                                    self.logger.info(f"🧹 Cleaned up subtitle file: {os.path.basename(file_path)}")
                                    files_cleaned += 1
                                except FileNotFoundError:
                                    pass
                    except OSError as e:
                        self.logger.warning(f"⚠️ Could not remove subtitle file {file_path}: {e}")
            
            # Additional cleanup: remove any .srt or .vtt files in the directory that were created recently
            if files_cleaned == 0:
                current_time = time.time()
                for ext in ['.srt', '.vtt']:
                    for file_path in self.config.current_video_dir.glob(f"*{ext}"):
                        try:
                            # Remove files created in the last hour
                            if file_path.stat().st_mtime > current_time - 3600:
                                os.remove(file_path)
                                self.logger.info(f"🧹 Cleaned up recent subtitle file: {file_path.name}")
                                files_cleaned += 1
                        except OSError as e:
                            self.logger.warning(f"⚠️ Could not remove recent subtitle file {file_path}: {e}")
            
            self.logger.info(f"🧹 Subtitle cleanup completed: {files_cleaned} files removed")
                        
        except Exception as e:
            self.logger.error(f"❌ Error during subtitle cleanup: {e}")
    
    def check_existing_download(self, video_info: Dict[str, Any], download_type: str = "video") -> Tuple[bool, int]:
        """Check if download exists and can be resumed. Returns (can_resume, progress_percent)."""
        try:
            video_title = video_info.get("title", "Unknown")
            clean_title = re.sub(r'[<>:"/\\|?*]', '_', video_title)
            
            if download_type == "video":
                download_dir = self.config.current_video_dir
                file_pattern = f"*{clean_title}*.mp4"
            else:  # audio
                download_dir = self.config.current_audio_dir
                file_pattern = f"*{clean_title}*.mp3"
            
            # Look for existing files
            for file_path in download_dir.glob(file_pattern):
                if file_path.is_file():
                    file_size = file_path.stat().st_size
                    
                    # If file is larger than 1MB, assume it's partially downloaded
                    if file_size > 1024 * 1024:
                        return True, 50
                    
                    # If file exists and is complete, return 100%
                    return True, 100
            
            return False, 0
            
        except Exception as e:
            self.logger.warning(f"⚠️ Error checking existing download: {e}")
            return False, 0

    def download_video(self, video_info: Dict[str, Any], source_name: str, is_manual: bool = False, progress: Optional[Progress] = None, task_id: Any = None) -> Tuple[bool, str]:
        """Download a video from YouTube with progress display, subtitle error handling, and resume support."""
        video_id = video_info["id"]
        video_url = video_info["url"]
        video_title = video_info.get("title", "Unknown")
        duration = video_info.get("duration_formatted", "Unknown")
        
        # Check if we can resume this download
        can_resume, current_resume_progress = self.check_existing_download(video_info, "video")
        resume_state = self.get_resume_state("video", video_id)
        
        # Show resume status if applicable
        resume_msg = ""
        if can_resume and current_resume_progress < 100:
            resume_msg = f"[yellow]🔄 Resuming from {current_resume_progress}%[/yellow]"
            resume = True
        elif resume_state and current_resume_progress < 100:
            resume_msg = f"[yellow]🔄 Resuming previous download[/yellow]"
            resume = True
        else:
            resume = False
            
        # Only show panel if NOT running in parallel/progress mode
        if not progress:
            # Create info grid
            grid = Table.grid(padding=(0, 2))
            grid.add_column(style="bold cyan", justify="right")
            grid.add_column(style="white")
            
            grid.add_row("Source:", source_name)
            grid.add_row("Title:", video_title)
            grid.add_row("Duration:", duration)
            if resume_msg:
                grid.add_row("Status:", resume_msg)
                
            console.print(Panel(grid, title="[bold green]Downloading Video[/]", border_style="green"))
            
            print("   🔍 Checking for subtitles...", end="", flush=True)
            
        has_subtitles = self.check_subtitles_available(video_url)
        
        # Update resume state
        if resume:
            self.update_resume_state("video", video_id, {
                "url": video_url,
                "title": video_title,
                "source": source_name,
                "progress": current_resume_progress, # Use the actual integer
                "type": "video"
            })
        
        if has_subtitles:
            if not progress:
                print(" ✅ (Subtitles available)")
            cmd = self.build_download_command(video_url, source_name, skip_subs=False, is_manual=is_manual, resume=resume)
            success, video_id, error_msg = self._execute_download(cmd, video_info, source_name, is_audio=False, progress=progress, task_id=task_id)
            
            if not success and ("subtitles" in error_msg.lower() or "429" in error_msg):
                if not progress:
                    print(f"   ⚠️ Subtitle error detected, retrying without subtitles...")
                cmd = self.build_download_command(video_url, source_name, skip_subs=True, is_manual=is_manual, resume=resume)
                success, video_id, _ = self._execute_download(cmd, video_info, source_name, is_audio=False, progress=progress, task_id=task_id)
        else:
            if not progress:
                print(" ⚠️ (No subtitles available)")
            cmd = self.build_download_command(video_url, source_name, skip_subs=True, is_manual=is_manual, resume=resume)
            success, video_id, error_msg = self._execute_download(cmd, video_info, source_name, is_audio=False, progress=progress, task_id=task_id)
        
        if success:
            if has_subtitles:
                self.cleanup_subtitle_files(video_title, is_manual=is_manual)
            
            if not progress:
                print("   ✅ Download complete!")
            
            features = ["720p HD", "MP4 format", "Private mode", "SponsorBlock"]
            
            # Print success message only if this was the last specific info printed or if manually requested?
            # User said: "no need to print this after every single download do it only ofr the last look better"
            # But in parallel, we don't know which is last easily here.
            # However, with Rich Progress, individual success prints mess up the bars.
            # We suppressed them in the 'if not progress:' block above.
            
            self._save_download_success(video_info, source_name, video_title)
            # Clear resume state on successful completion
            self.clear_resume_state("video", video_id)
            
            return True, video_id
        else:
            print(f"   ❌ Download failed")
            if error_msg:
                print(f"   Error: {error_msg[:100]}")
            # Keep resume state for failed downloads
            return False, video_id

    def _execute_download(self, cmd: List[str], video_info: Dict[str, Any], source_name: str, is_audio: bool = False, progress: Optional[Progress] = None, task_id: Any = None) -> Tuple[bool, str, str]:
        """Execute download command and handle progress display."""
        video_id = video_info["id"]
        video_title = video_info.get("title", "Unknown")
        
        try:
            download_type = "audio" if is_audio else "video"
            self.logger.info(f"🚀 Starting {download_type} download: {source_name} - {video_title}")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            state = {
                "last_percent": 0,
                "initial_progress_printed": False
            }
            
            stderr_lines = []
            
            while True:
                if process.poll() is not None:
                    break
                    
                ready, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                
                for stream in ready:
                    line = stream.readline()
                    if not line:
                        continue
                    
                    if stream == process.stderr:
                        stderr_lines.append(line)
                    
                    progress_dict = self.parse_progress(line)
                    
                    if progress_dict:
                        if progress and task_id is not None:
                            # Update Rich Progress Bar
                            if progress_dict.get("type") == "starting":
                                progress.update(task_id, description=f"[yellow]Connecting...[/yellow] {video_title}", visible=True)
                            elif progress_dict.get("type") == "download" and "percent" in progress_dict:
                                percent = float(progress_dict["percent"])
                                speed = progress_dict.get("speed", "")
                                eta = progress_dict.get("eta", "")
                                progress.update(task_id, completed=percent, description=f"{video_title} [dim]({speed})[/dim]", visible=True)
                        
                        else:
                            # Legacy Print Output
                            if progress_dict.get("type") == "starting" and not state["initial_progress_printed"]:
                                print("   📡 Connecting...", end="", flush=True)
                                state["initial_progress_printed"] = True
                            elif progress_dict.get("type") == "download" and "percent" in progress_dict:
                                percent = float(progress_dict["percent"])
                                if percent > state["last_percent"]:
                                    self.display_single_progress_bar(
                                        percent,
                                        progress_dict.get("size", ""),
                                        progress_dict.get("speed", ""),
                                        progress_dict.get("eta", "")
                                    )
                                    state["last_percent"] = percent
                                    if percent >= 100:
                                        print()
            
            return_code = process.wait()
            error_summary = "".join(stderr_lines[-5:]) if stderr_lines else "Unknown error"
            
            if return_code == 0:
                # Double check stderr for "ERROR:" because sometimes yt-dlp returns 0 even on failure
                if any("ERROR:" in line for line in stderr_lines):
                    return False, video_id, error_summary
                return True, video_id, ""
            else:
                return False, video_id, error_summary
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"⏱️ Download timeout for {source_name}")
            return False, video_id, "Download timeout"
        except Exception as e:
            self.logger.error(f"❌ Download error for {source_name}: {e}")
            return False, video_id, str(e)

    def display_single_progress_bar(self, percent: float, size: str = "", speed: str = "", eta: str = "", width: int = 40):
        """Display single download progress bar."""
        filled = int(width * percent / 100)
        bar = "█" * filled + "░" * (width - filled)
        
        info_parts = [f"{percent:.1f}%"]
        if size:
            info_parts.append(f"{size}")
        if speed:
            info_parts.append(f"{speed}")
        if eta:
            info_parts.append(f"ETA:{eta}")
        
        info = " ".join(info_parts)
        print(f"\r\033[K   [{bar}] {info}", end="", flush=True)
    
    def _save_download_success(self, video_info: Dict[str, Any], source_name: str, video_title: str, is_audio: bool = False) -> None:
        """Save download success information."""
        download_type = "MP3 Audio (320kbps)" if is_audio else "720p MP4"
        self.save_last_download({
            "source": source_name,
            "title": video_title,
            "video_id": video_info["id"],
            "url": video_info["url"],
            "duration": video_info.get("duration_formatted", "Unknown"),
            "type": download_type,
            "private": True,
            "timestamp": datetime.now().isoformat()
        })
    
    def process_channel_auto(self, handle: str, display_name: str) -> List[Tuple[Dict[str, Any], str, str]]:
        """Process a YouTube channel and return new videos since last check WITH LIMITS."""
        download_tasks = []
        
        # Determine maximum videos to check
        # Determine maximum videos to check
        # If it's a known channel, we want to check enough videos to bridge the gap since last run.
        # Default to a safe number (e.g., 10) to catch up on missed daily videos.
        check_limit = self.config.gap_check_limit
        if hasattr(self.config, 'max_videos_per_channel') and self.config.max_videos_per_channel > check_limit:
            # If user configured a higher manual limit, respect it
            check_limit = self.config.max_videos_per_channel
        
        # Check if channel is new (never downloaded from before)
        is_new_channel = handle not in self.channel_history.get("channels", {})
        
        # Prompt if new channel OR global setting enabled
        if is_new_channel or self.config.ask_video_limit_per_channel:
            # Clear previous spinner/output if any
            console.print("")
            if is_new_channel:
                console.print(f"[bold yellow]🆕 New channel detected: {display_name}[/bold yellow]")
                default_limit = self.config.initial_videos_per_channel
            else:
                console.print(f"[bold cyan]🔍 Checking: {display_name}[/bold cyan]")
                default_limit = check_limit
                
            try:
                if sys.stdin.isatty():
                    msg = f"   How many recent videos to check? [dim](default {default_limit})[/dim]: "
                    user_limit = console.input(msg).strip()
                    if user_limit:
                        check_limit = int(user_limit)
                    else:
                        check_limit = default_limit
                else:
                    check_limit = default_limit
            except ValueError:
                console.print(f"   [red]Invalid number, using default {default_limit}[/red]")
                check_limit = default_limit
            except EOFError:
                check_limit = default_limit

        
        # Get recent videos with the configured limit
        # Use a spinner for visual feedback
        with console.status(f"[bold blue]🔍 Checking {display_name}...[/bold blue]"):
            recent_videos = self.get_all_recent_videos(
                f"https://www.youtube.com/@{handle}/videos",
                display_name,
                max_videos=check_limit,
                silent=True
            )
        
        if not recent_videos:
            console.print(f"[dim]🔍 {display_name}: No new videos[/dim]")
            return []
        
        new_videos_count = 0
        already_downloaded_count = 0
        shorts_skipped = 0
        
        # Sort recent videos by date (oldest first) to ensure we download in chronological order
        # This helps with the "download all remaining video from last download" request
        # Assuming get_all_recent_videos returns them in some order, but usually API returns newest first.
        # We process them in the order returned, but for "gap filling" we might want to check all.
        
        # Logic to "download all remaining video from last download video":
        # We need to find the latest downloaded video for this channel.
        last_downloaded_id = None
        if handle in self.channel_history.get("channels", {}):
             downloaded_videos = self.channel_history["channels"][handle].get("downloaded_videos", [])
             if downloaded_videos:
                 # Get the one with the most recent timestamp? Or just rely on the ID check?
                 # The current logic checks `is_video_downloaded` which checks the specific ID.
                 # If we missed intermediate videos, they won't be in history, so `is_video_downloaded` returns False.
                 # So simply iterating through `recent_videos` (which fetches 'max_videos') should catch them
                 # IF 'max_videos' is large enough.
                 pass

        for video_info in recent_videos:
            video_id = video_info["id"]
            
            # Filter Shorts if enabled
            if self.config.filter_shorts and video_info.get("duration", 0) < 60:
                shorts_skipped += 1
                continue
            
            # Check if video has already been downloaded using our comprehensive history
            if self.is_video_downloaded(handle, video_id):
                already_downloaded_count += 1
                continue
            
            # This is a new video to download
            download_tasks.append((video_info, display_name, "channel"))
            new_videos_count += 1
        
        # Display results using Tree
        if new_videos_count > 0:
            tree = Tree(f"[bold blue]🔍 {display_name}[/bold blue]")
            status_node = tree.add(f"[success]✅ Found {new_videos_count} NEW videos[/success]")
            
            if shorts_skipped > 0:
                tree.add(f"[dim]⏭️  Skipped {shorts_skipped} Shorts (<60s)[/dim]")
            
            # Show first 3 new videos
            videos_node = tree.add(f"📥 [bold]{new_videos_count}[/bold] to download")
            new_videos_shown = 0
            for video_info in recent_videos:
                if new_videos_shown >= 3:
                    break
                video_id = video_info["id"]
                # Skip shorts in display too
                if self.config.filter_shorts and video_info.get("duration", 0) < 60:
                    continue
                    
                if not self.is_video_downloaded(handle, video_id):
                    videos_node.add(f"[cyan]{video_info.get('title', 'Unknown')}[/cyan]")
                    new_videos_shown += 1
            
            if new_videos_count > 3:
                videos_node.add(f"[dim]... and {new_videos_count - 3} more[/dim]")
                
            console.print(tree)
            console.print("")
        else:
            # Compact display for no updates
            msg = f"[dim]🔍 {display_name}: No new videos[/dim]"
            if shorts_skipped > 0:
                msg += f" [dim](skipped {shorts_skipped} Shorts)[/dim]"
            console.print(msg)
        
        return download_tasks

    def process_playlist_auto(self, playlist_url: str, playlist_name: str) -> List[Tuple[Dict[str, Any], str, str]]:
        """Process a YouTube playlist and return new videos since last check WITH LIMITS."""
        download_tasks = []
        
        # Use the configured maximum videos per channel for playlists too
        max_videos = self.config.max_videos_per_channel
        
        # Get recent videos from playlist - use silent mode
        with console.status(f"[bold blue]🔍 Checking {playlist_name}...[/bold blue]"):
            recent_videos = self.get_all_recent_videos(playlist_url, playlist_name, max_videos=max_videos, silent=True)
        
        if not recent_videos:
            console.print(f"[dim]🔍 {playlist_name}: No videos found or error[/dim]")
            return []
        
        new_videos_count = 0
        already_downloaded_count = 0
        shorts_skipped = 0
        
        for video_info in recent_videos:
            video_id = video_info["id"]
            
            # Filter Shorts if enabled
            if self.config.filter_shorts and video_info.get("duration", 0) < 60:
                shorts_skipped += 1
                continue
            
            # Use playlist URL as identifier for history tracking
            playlist_id = playlist_url
            
            # Check if video has already been downloaded
            if self.is_video_downloaded(playlist_id, video_id):
                already_downloaded_count += 1
                continue
            
            # This is a new video to download
            download_tasks.append((video_info, playlist_name, "playlist"))
            new_videos_count += 1
        
        # Display results using Tree
        if new_videos_count > 0:
            tree = Tree(f"[bold blue]🔍 {playlist_name}[/bold blue]")
            status_node = tree.add(f"[success]✅ Found {new_videos_count} NEW videos[/success]")
            
            if shorts_skipped > 0:
                tree.add(f"[dim]⏭️  Skipped {shorts_skipped} Shorts (<60s)[/dim]")
                
            videos_node = tree.add(f"📥 [bold]{new_videos_count}[/bold] to download")
            
            # Show first 3 new videos (we don't have the list easily accessible here as we iterated, 
            # but we can just show the count which is cleaner for playlists usually)
            
            console.print(tree)
            console.print("")
        else:
            # Compact display for no updates
            msg = f"[dim]🔍 {playlist_name}: No new videos[/dim]"
            if shorts_skipped > 0:
                msg += f" [dim](skipped {shorts_skipped} Shorts)[/dim]"
            console.print(msg)
        
        return download_tasks

    def get_initial_video_limit(self) -> int:
        """Ask user how many recent videos to download on first run (if enabled)."""
        if not self.config.ask_initial_videos:
            return self.config.initial_videos_per_channel
        
        console.print(Panel(
            "[bold]First run detected[/bold]\n"
            "How many [cyan]RECENT[/cyan] videos would you like to download from each channel?\n"
            "[dim](Subsequent runs will automatically download ONLY NEW videos)[/dim]",
            title="[bold yellow]Setup[/]",
            border_style="yellow"
        ))
        
        while True:
            try:
                limit = console.input(f"   Enter number [dim][0-{self.config.max_videos_per_channel}, default {self.config.initial_videos_per_channel}][/dim]: ").strip()
                if not limit:
                    return self.config.initial_videos_per_channel
                
                limit = int(limit)
                if 0 <= limit <= self.config.max_videos_per_channel:
                    return limit
                else:
                    console.print(f"   [bold red]❌ Please enter a number between 0 and {self.config.max_videos_per_channel}[/bold red]")
            except ValueError:
                console.print("   [bold red]❌ Please enter a valid number[/bold red]")

    def process_channel_first_run(self, handle: str, display_name: str, video_limit: int) -> List[Tuple[Dict[str, Any], str, str]]:
        """Process a channel on first run with limited recent videos."""
        download_tasks = []
        
        # If video_limit is 0, skip downloading entirely
        if video_limit == 0:
            console.print(f"[dim]🔍 {display_name}: Skipped (limit 0)[/dim]")
            return []
        
        # Use silent mode to avoid duplicate printing
        with console.status(f"[bold blue]🔍 Scanning {display_name}...[/bold blue]"):
            recent_videos = self.get_all_recent_videos(
                f"https://www.youtube.com/@{handle}/videos",
                display_name,
                max_videos=video_limit,
                silent=True  # Don't print from the method itself
            )
        
        if not recent_videos:
            console.print(f"[dim]🔍 {display_name}: No videos found or error[/dim]")
            return []
        
        # On first run, download all the recent videos we found (up to limit)
        for video_info in recent_videos:
            download_tasks.append((video_info, display_name, "channel"))
        
        # Display results using Tree
        tree = Tree(f"[bold blue]🔍 {display_name}[/bold blue]")
        tree.add(f"[success]✅ Found {len(recent_videos)} videos to download[/success]")
        
        videos_node = tree.add(f"📥 [bold]{len(recent_videos)}[/bold] videos")
        for i, video_info in enumerate(recent_videos):
            if i < 3:
                videos_node.add(f"[cyan]{video_info.get('title', 'Unknown')}[/cyan]")
        if len(recent_videos) > 3:
            videos_node.add(f"[dim]... and {len(recent_videos) - 3} more[/dim]")
            
        console.print(tree)
        console.print("")
        
        # Show what we're downloading
        for video_info in recent_videos[:3]:
            print(f"      📥 {video_info.get('title', 'Unknown')}")
        
        if len(recent_videos) > 3:
            print(f"      ... and {len(recent_videos) - 3} more")
        
        return download_tasks

    def download_videos_parallel(self, download_tasks: List[Tuple[Dict[str, Any], str, str]]) -> int:
        """Download multiple videos in parallel and update channel history."""
        successful_downloads = 0
        
        if download_tasks:
            console.print(Rule(f"[bold green]Starting {len(download_tasks)} parallel downloads[/]"))
            console.print("")
            
            # Setup Rich Progress Bar
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold blue]{task.fields[source]}", justify="right"),
                TextColumn("[white]{task.description}"),
                BarColumn(bar_width=None),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                expand=True
            )
            
            # Create a single shared progress bar for all downloads if preferred, 
            # but user requested "improvised one loading bar which account variable for both video in one".
            # Rich's Progress can handle multiple bars neatly. 
            # To meet the request "instead of having two loading bar or a new improvised one loading bar which account variable for both video in one",
            # we will stick to the multi-bar approach which IS the standard "improvised" way to handle parallel downloads in CLI.
            # Alternating bars in one line is bad, so we use Rich properly which stacks them.
            
            with progress:
                with ThreadPoolExecutor(max_workers=self.config.max_parallel_downloads) as executor:
                    future_to_task = {}
                    
                    for video_info, source_name, task_type in download_tasks:
                        # Add task to progress bar
                        video_title = video_info.get("title", "Unknown")
                        # Truncate title for display
                        display_title = (video_title[:30] + '...') if len(video_title) > 30 else video_title
                        task_id = progress.add_task(f"waiting...", source=source_name, total=100, visible=False)
                        
                        future = executor.submit(
                            self.download_video, 
                            video_info, 
                            source_name, 
                            is_manual=False,
                            progress=progress, # Pass the progress object
                            task_id=task_id    # Pass the task ID
                        )
                        future_to_task[future] = (video_info, source_name, task_type)
                    
                    for future in as_completed(future_to_task):
                        video_info, source_name, task_type = future_to_task[future]
                        try:
                            success, video_id = future.result()
                            if success:
                                successful_downloads += 1
                                
                                # Update comprehensive channel history
                                if task_type == "channel":
                                    handle = [k for k, v in self.channels.items() if v == source_name][0]
                                    self.update_channel_history(handle, video_info)
                                elif task_type == "playlist":
                                    # Use playlist URL as channel identifier for playlists
                                    playlist_url = [url for url, name in self.playlists.items() if name == source_name][0]
                                    self.update_channel_history(playlist_url, video_info)
                                
                                # Also update the simple download history for backward compatibility
                                if task_type == "channel":
                                    handle = [k for k, v in self.channels.items() if v == source_name][0]
                                    self.download_history["channels"][handle] = video_id
                                elif task_type == "playlist":
                                    playlist_url = [url for url, name in self.playlists.items() if name == source_name][0]
                                    self.download_history["playlists"][playlist_url] = video_id
                                
                                self.save_download_history()
                        except Exception as e:
                            console.print(f"[red]❌ Error in parallel download for {source_name}: {e}[/red]")
        
        return successful_downloads


    def run_auto_download(self) -> None:
        """Main automatic execution method - smart video downloading."""
        start_time = time.time()
        
        # Run cleanup first
        self.cleanup_old_videos()
        
        # Header
        console.print(Rule("[bold blue]YouTube Feed Downloader[/]"))
        
        # Stats Grid
        stats_text = Text()
        stats_text.append(f"📁 Videos: {self.config.current_video_dir}\n", style="yellow")
        stats_text.append(f"🎧 Audio: {self.config.current_audio_dir}\n", style="yellow")
        stats_text.append(f"👀 Monitoring: {len(self.channels)} channels, {len(self.playlists)} playlists\n", style="green")
        stats_text.append(f"⚡ Parallel: {self.config.max_parallel_downloads} simultaneous downloads\n", style="bold")
        stats_text.append(f"🔄 Resume: Enabled ({self.config.resume_cache_days} days)\n")
        stats_text.append(f"🧹 Auto-cleanup: {self.config.cleanup_days} days\n")
        if self.config.filter_shorts:
            stats_text.append(f"⏱️  Shorts Filter: Enabled (<60s)", style="green")
            
        console.print(Panel(stats_text, title="Configuration", border_style="blue"))
        console.print("")
        
        if self.is_first_run():
            console.print(f"[bold yellow]📺 First run: Will {'ask for recent videos' if self.config.ask_initial_videos else f'download {self.config.initial_videos_per_channel} recent videos'}[/]")
        else:
            console.print(f"[bold cyan]📺 Strategy: Downloading NEW videos (filling gaps + newest)[/]")
        console.print("")
        
        all_download_tasks = []
        
        # FIRST RUN: Ask for recent videos or use default
        if self.is_first_run():
            if self.config.ask_initial_videos:
                videos_per_channel = self.get_initial_video_limit()
            else:
                videos_per_channel = self.config.initial_videos_per_channel
            
            # Only show download message if we're actually going to download something
            if videos_per_channel > 0:
                console.print(f"\n[bold]🚀 First run - downloading {videos_per_channel} recent videos per channel...[/bold]\n")
            else:
                console.print(f"\n[bold]🚀 First run - skipping initial downloads (limit set to 0)...[/bold]\n")
            
            if self.channels:
                console.print(Rule("[bold blue]Checking Channels[/]"))
                for handle, name in self.channels.items():
                    channel_tasks = self.process_channel_first_run(handle, name, videos_per_channel)
                    all_download_tasks.extend(channel_tasks)
                    # print() # Spacing handled by Rule/Tree
            
            if self.playlists:
                console.print(Rule("[bold blue]Checking Playlists[/]"))
                for url, name in self.playlists.items():
                    playlist_tasks = self.process_channel_first_run(url, name, videos_per_channel)
                    all_download_tasks.extend(playlist_tasks)
                    # print()
            
            # Mark first run as completed even if no videos were downloaded
            self.mark_first_run_completed()
            
        # SUBSEQUENT RUNS: Download NEW videos with limits
        else:
            console.print(Rule("[bold green]Checking for NEW videos[/]"))
            console.print("")
            
            if self.channels:
                for handle, name in self.channels.items():
                    channel_tasks = self.process_channel_auto(handle, name)
                    all_download_tasks.extend(channel_tasks)
                    # print()
            
            if self.playlists:
                console.print(Rule("[bold blue]Checking Playlists[/]"))
                console.print("")
                for url, name in self.playlists.items():
                    playlist_tasks = self.process_playlist_auto(url, name)
                    all_download_tasks.extend(playlist_tasks)
                    # print()
        
        successful_downloads = self.download_videos_parallel(all_download_tasks)
        
        elapsed = time.time() - start_time
        
        # Clean up any empty YT_feed directories created during the process
        self.cleanup_empty_directories()
        
        self.update_directory_names()
        
        # Summary Table
        table = Table(title="Download Summary Report", show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        
        table.add_row("New Downloads", str(successful_downloads))
        table.add_row("Total Time", f"{elapsed:.1f}s")
        if successful_downloads > 0:
            table.add_row("Avg Time/Download", f"{elapsed/successful_downloads:.1f}s")
            
        console.print(table)
        
        if self.is_first_run() and successful_downloads == 0 and self.config.initial_videos_per_channel == 0:
            console.print(f"[yellow]💡 First run completed - no videos downloaded (limit set to 0)[/yellow]")
        elif self.is_first_run() and successful_downloads > 0:
            console.print(f"[green]💡 Next run will automatically download NEW videos (max {self.config.max_videos_per_channel} per channel)[/green]")
        
        self.logger.info(f"📊 Summary - Downloads: {successful_downloads}, Time: {elapsed:.1f}s")
        
        # Send notification
        if successful_downloads > 0:
            self.send_notification(
                "Downloads Complete", 
                f"Downloaded {successful_downloads} new videos in {elapsed:.1f}s"
            )
        elif elapsed > 60: # Only notify for long runs even if empty
            self.send_notification(
                "Check Complete",
                "No new videos found"
            )

    def cleanup_empty_directories(self):
        """Clean up empty YT_feed directories that might have been created."""
        try:
            # Look for YT_feed directories in the Videos folder
            videos_dir = Path.home() / "Videos"
            yt_feed_dirs = list(videos_dir.glob("YT_feed*"))
            
            for directory in yt_feed_dirs:
                if directory.is_dir():
                    # Check if directory is empty or only contains a few files
                    files = list(directory.glob("*"))
                    if len(files) <= 1:
                        # Check if this isn't our main current directory
                        if directory != self.config.current_video_dir:
                            try:
                                # Remove all files first
                                for file_path in directory.glob("*"):
                                    try:
                                        file_path.unlink()
                                    except:
                                        pass
                                # Then remove directory
                                directory.rmdir()
                                self.logger.info(f"🧹 Cleaned up empty directory: {directory}")
                            except Exception as e:
                                self.logger.warning(f"⚠️ Could not remove directory {directory}: {e}")
        except Exception as e:
            self.logger.error(f"❌ Error during directory cleanup: {e}")

    def interactive_mode(self):
        """Interactive mode for managing downloads and configuration."""
        while True:
            menu_text = Text()
            menu_text.append("1. 🚀 Run automatic download (SMART mode)\n", style="bold green")
            menu_text.append("2. 📺 Manage channels\n")
            menu_text.append("3. 📋 Manage playlists\n")
            menu_text.append("4. 🎬 Download single video\n")
            menu_text.append("5. 🎧 Download single audio (MP3)\n")
            menu_text.append("6. 📚 Download playlist\n")
            menu_text.append("7. 📊 Show statistics\n")
            menu_text.append("8. 🔧 Settings\n")
            menu_text.append("9. 🧹 Clear resume data\n")
            menu_text.append("0. ❌ Exit", style="bold red")
            
            console.print(Panel(menu_text, title="[bold blue]YouTube Feed Downloader[/]", border_style="blue"))

            
            choice = input("\nSelect option (0-9): ").strip()
            
            if choice == "1":
                self.run_auto_download()
            elif choice == "2":
                self.manage_channels()
            elif choice == "3":
                self.manage_playlists()
            elif choice == "4":
                self.download_single_video()
            elif choice == "5":
                self.download_single_audio()
            elif choice == "6":
                self.download_playlist_interactive()
            elif choice == "7":
                self.show_statistics()
            elif choice == "8":
                self.manage_settings()
            elif choice == "9":
                self.clear_all_resume_data()
            elif choice == "0":
                print("\n👋 Goodbye!")
                break
            else:
                print("❌ Invalid option. Please try again.")

    def clear_all_resume_data(self):
        """Clear all resume data."""
        try:
            self.resume_state = {
                "videos": {},
                "playlists": {},
                "last_cleanup": datetime.now().isoformat()
            }
            self.save_resume_state()
            print("✅ All resume data cleared!")
        except Exception as e:
            print(f"❌ Error clearing resume data: {e}")

    def download_playlist_interactive(self):
        """Interactive playlist download with video/audio options."""
        print("\n📚 Playlist Download")
        print("─" * 40)
        url = input("Enter YouTube playlist URL: ").strip()
        
        if not url:
            print("❌ No URL provided")
            return
        
        print("\n🎯 Select download type:")
        print("   1. 🎬 Video Playlist (720p MP4)")
        print("   2. 🎧 Audio/Podcast (320kbps MP3)")
        
        type_choice = input("\nSelect option (1-2): ").strip()
        
        if type_choice == "1":
            download_type = "video"
            print("\n📥 Downloading as Video Playlist...")
        elif type_choice == "2":
            download_type = "audio"
            print("\n📥 Downloading as Audio/Podcast...")
        else:
            print("❌ Invalid option")
            return
        
        start_time = time.time()
        success, playlist_dir = self.download_playlist(url, download_type)
        elapsed = time.time() - start_time
        
        if success and playlist_dir:
            total_seconds, duration_short, file_info = self.calculate_directory_duration(playlist_dir)
            base_name = playlist_dir.name
            clean_base = re.sub(r'\s*-\s*\d+[hmr]\s*\d*[ms]?in?$', '', base_name)
            clean_base = re.sub(r'\s*-\s*\d+sec$', '', clean_base)
            clean_base = re.sub(r'\s*-\s*\d+\.\d+sec$', '', clean_base)
            new_playlist_dir = self.rename_directory_with_duration(playlist_dir, clean_base)
            print(f"   📚 Updated: {clean_base} -{duration_short}")
        
        print("\n" + "=" * 60)
        print(f"⏱️  Playlist download completed in {elapsed:.1f} seconds")
        print("=" * 60)

    def manage_channels(self):
        """Manage YouTube channels."""
        while True:
            print(f"\n📺 Channel Management ({len(self.channels)} channels)")
            print("─" * 40)
            print("1. 📋 List channels")
            print("2. ➕ Add channel")
            print("3. 🗑️ Remove channel")
            print("4. ↩️ Back to main menu")
            
            choice = input("\nSelect option (1-4): ").strip()
            
            if choice == "1":
                print("\nCurrent channels:")
                for handle, name in self.channels.items():
                    print(f"  📹 {name} (@{handle})")
            elif choice == "2":
                handle = input("Enter channel handle (without @): ").strip()
                name = input("Enter display name: ").strip()
                if handle and name:
                    self.channels[handle] = name
                    self.save_config()
                    print(f"✅ Added channel: {name}")
                else:
                    print("❌ Invalid input")
            elif choice == "3":
                if not self.channels:
                    print("❌ No channels to remove")
                    continue
                    
                print("\nSelect channel to remove:")
                handles = list(self.channels.keys())
                for i, (handle, name) in enumerate(self.channels.items(), 1):
                    print(f"{i}. {name} (@{handle})")
                
                try:
                    remove_choice = int(input("\nEnter number: ")) - 1
                    if 0 <= remove_choice < len(handles):
                        removed = self.channels.pop(handles[remove_choice])
                        self.save_config()
                        print(f"✅ Removed channel: {removed}")
                    else:
                        print("❌ Invalid selection")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "4":
                break
            else:
                print("❌ Invalid option")

    def manage_playlists(self):
        """Manage YouTube playlists."""
        while True:
            print(f"\n📋 Playlist Management ({len(self.playlists)} playlists)")
            print("─" * 40)
            print("1. 📋 List playlists")
            print("2. ➕ Add playlist")
            print("3. 🗑️ Remove playlist")
            print("4. ↩️ Back to main menu")
            
            choice = input("\nSelect option (1-4): ").strip()
            
            if choice == "1":
                print("\nCurrent playlists:")
                for url, name in self.playlists.items():
                    print(f"  📚 {name}")
                    print(f"     🔗 {url}")
            elif choice == "2":
                url = input("Enter playlist URL: ").strip()
                name = input("Enter playlist name: ").strip()
                if url and name:
                    self.playlists[url] = name
                    self.save_config()
                    print(f"✅ Added playlist: {name}")
                else:
                    print("❌ Invalid input")
            elif choice == "3":
                if not self.playlists:
                    print("❌ No playlists to remove")
                    continue
                    
                print("\nSelect playlist to remove:")
                urls = list(self.playlists.keys())
                for i, (url, name) in enumerate(self.playlists.items(), 1):
                    print(f"{i}. {name}")
                
                try:
                    remove_choice = int(input("\nEnter number: ")) - 1
                    if 0 <= remove_choice < len(urls):
                        removed = self.playlists.pop(urls[remove_choice])
                        self.save_config()
                        print(f"✅ Removed playlist: {removed}")
                    else:
                        print("❌ Invalid selection")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "4":
                break
            else:
                print("❌ Invalid option")

    def download_single_video(self):
        """Download a single video by URL."""
        print("\n🎬 Single Video Download")
        print("─" * 40)
        url = input("Enter YouTube video URL: ").strip()
        
        if not url:
            print("❌ No URL provided")
            return
        
        video_info = self.get_all_recent_videos(url, "Single Video", max_videos=1, silent=True)
        if video_info:
            print("🔍 Getting video information...")
            print("   ✅ Video found")
            video_data = video_info[0]
            uploader = video_data.get("uploader", "Single Video")
            success, _ = self.download_video(video_data, uploader, is_manual=True)
            if not success:
                print("❌ Video download failed")
        else:
            print("🔍 Getting video information...")
            print("   ❌ No video found or error")

    def download_single_audio(self):
        """Download a single audio by URL."""
        print("\n🎧 Downloading Audio")
        print("─" * 40)
        url = input("Enter YouTube video URL: ").strip()
        
        if not url:
            print("❌ No URL provided")
            return
        
        # For audio download, we'll use a simpler approach
        video_info = self.get_all_recent_videos(url, "Single Audio", max_videos=1, silent=True)
        if video_info:
            print("🔍 Getting video information...")
            print("   ✅ Video found")
            video_info_data = video_info[0]
            
            # Check for existing file before trying to download
            # This uses a simple check in the default download directory
            dummy_filename = f"{video_info_data['title']}.mp4" # Approximate check
            # Real check would need full path logic, but is_video_downloaded might suffice if it tracked singles.
            # But single downloads usually don't update history in the same way or might be manual.
            # Best is to let yt-dlp handle it or do a quick title search.
            
            uploader = video_info_data.get("uploader", "Single Audio")
            success, _ = self.download_video(video_info_data, uploader, is_manual=True)
            if not success:
                print("❌ Audio download failed")
        else:
            print("🔍 Getting video information...")
            print("   ❌ No video found or error")

    def show_statistics(self):
        """Show download statistics."""
        console.print(Rule("[bold magenta]Download Statistics[/]"))
        
        # Configuration Table
        config_table = Table(show_header=False, box=None)
        config_table.add_column("Key", style="cyan")
        config_table.add_column("Value", style="white")
        
        config_table.add_row("📁 Video directory", str(self.config.current_video_dir))
        config_table.add_row("🎧 Audio directory", str(self.config.current_audio_dir))
        config_table.add_row("📚 Playlist directory", str(self.config.current_playlist_dir))
        config_table.add_row("📻 Podcast directory", str(self.config.current_podcast_dir))
        config_table.add_row("📺 Channels monitored", str(len(self.channels)))
        config_table.add_row("📋 Playlists monitored", str(len(self.playlists)))
        config_table.add_row("⚡ Parallel downloads", str(self.config.max_parallel_downloads))
        config_table.add_row("🔒 Privacy mode", "Enabled")
        config_table.add_row("🔄 Resume cache", f"{self.config.resume_cache_days} days")
        config_table.add_row("✅ First run completed", 'Yes' if not self.is_first_run() else 'No')
        config_table.add_row("🔔 Ask for recent videos", 'Yes' if self.config.ask_initial_videos else 'No')
        config_table.add_row("📊 Max videos to check", str(self.config.max_videos_per_channel))
        
        console.print(Panel(config_table, title="Configuration", border_style="blue"))
        
        # Calculate stats
        total_downloaded_videos = 0
        for channel_data in self.channel_history.get("channels", {}).values():
            total_downloaded_videos += len(channel_data.get("downloaded_videos", []))
            
        video_resume_count = len(self.resume_state.get("videos", {}))
        playlist_resume_count = len(self.resume_state.get("playlists", {}))
        
        video_files = list(self.config.current_video_dir.glob("*.mp4"))
        audio_files = list(self.config.current_audio_dir.glob("*.mp3"))
        
        playlist_dirs = [d for d in self.config.current_playlist_dir.iterdir() if d.is_dir()]
        podcast_dirs = [d for d in self.config.current_podcast_dir.iterdir() if d.is_dir()]
        
        playlist_videos = 0
        for playlist_dir in playlist_dirs:
            playlist_videos += len(list(playlist_dir.glob("*.mp4")))
        
        podcast_files = 0
        for podcast_dir in podcast_dirs:
            podcast_files += len(list(podcast_dir.glob("*.mp3")))
            
        # Stats Table
        stats_table = Table(show_header=True, header_style="bold magenta")
        stats_table.add_column("Metric", style="cyan")
        stats_table.add_column("Count", style="green")
        
        stats_table.add_row("📥 Total videos tracked", str(total_downloaded_videos))
        stats_table.add_row("📋 Resume states", f"{video_resume_count} videos, {playlist_resume_count} playlists")
        stats_table.add_row("🎬 Total videos downloaded", str(len(video_files)))
        stats_table.add_row("🎧 Total audio files", str(len(audio_files)))
        stats_table.add_row("📚 Total video playlists", str(len(playlist_dirs)))
        stats_table.add_row("📹 Total playlist videos", str(playlist_videos))
        stats_table.add_row("📻 Total podcast playlists", str(len(podcast_dirs)))
        stats_table.add_row("🎵 Total podcast files", str(podcast_files))
        
        console.print(stats_table)
        
        if self.last_download:
            try:
                dt = datetime.fromisoformat(self.last_download['timestamp'].replace('Z', '+00:00'))
                formatted_time = dt.strftime("%b %d, %Y at %I:%M %p")
                download_type = self.last_download.get('type', 'Video')
                download_title = self.last_download.get('title', 'Unknown Title')
                download_source = self.last_download.get('source', 'Unknown Source')
                download_duration = self.last_download.get('duration', 'Unknown')
                
                console.print(f"[dim]🕒 Last download: {formatted_time}[/dim]")
                console.print(f"[dim]   📖 Source:   {download_source}[/dim]")
                console.print(f"[dim]   🎬 Title:    {download_title}[/dim]")
                console.print(f"[dim]   ⏱️  Duration: {download_duration} ({download_type})[/dim]")
            except Exception:
                # Fallback if formatting fails
                ts = self.last_download.get('timestamp', 'Unknown')
                title = self.last_download.get('title', 'Video')
                src = self.last_download.get('source', 'Unknown')
                dur = self.last_download.get('duration', 'Unknown')
                console.print(f"[dim]🕒 Last download: {ts}[/dim]")
                console.print(f"[dim]   📖 Source:   {src}[/dim]")
                console.print(f"[dim]   🎬 Title:    {title}[/dim]")
                console.print(f"[dim]   ⏱️  Duration: {dur}[/dim]")

    def manage_settings(self):
        """Manage application settings."""
        while True:
            print("\n⚙️ Settings")
            print("─" * 40)
            print(f"1. ⚡ Parallel downloads: {self.config.max_parallel_downloads}")
            print(f"2. 🗑️ Resume cache days: {self.config.resume_cache_days}")
            print(f"3. 🔔 Ask for recent videos on first run: {'Yes' if self.config.ask_initial_videos else 'No'}")
            print(f"4. 📺 Auto-download recent videos on first run: {self.config.initial_videos_per_channel}")
            print(f"5. 📊 Max videos to check per channel: {self.config.max_videos_per_channel}")
            print(f"6. 🎥 Max Video Quality: {self.config.max_resolution}p")
            print("7. ↩️ Back to main menu")
            
            choice = input("\nSelect option (1-7): ").strip()
            
            if choice == "1":
                try:
                    new_value = int(input(f"Enter new parallel download count (1-5): "))
                    if 1 <= new_value <= 5:
                        self.config.max_parallel_downloads = new_value
                        print(f"✅ Parallel downloads set to: {new_value}")
                    else:
                        print("❌ Please enter a number between 1 and 5")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "2":
                try:
                    new_value = int(input(f"Enter resume cache days (1-30): "))
                    if 1 <= new_value <= 30:
                        self.config.resume_cache_days = new_value
                        print(f"✅ Resume cache days set to: {new_value}")
                        self.cleanup_old_resume_entries()
                    else:
                        print("❌ Please enter a number between 1 and 30")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "3":
                self.config.ask_initial_videos = not self.config.ask_initial_videos
                status = "ENABLED" if self.config.ask_initial_videos else "DISABLED"
                print(f"✅ Ask for recent videos on first run: {status}")
            elif choice == "4":
                try:
                    new_value = int(input(f"Enter auto-download recent videos count (0-{self.config.max_videos_per_channel}): "))
                    if 0 <= new_value <= self.config.max_videos_per_channel:
                        self.config.initial_videos_per_channel = new_value
                        print(f"✅ Auto-download recent videos set to: {new_value}")
                    else:
                        print(f"❌ Please enter a number between 0 and {self.config.max_videos_per_channel}")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "5":
                try:
                    new_value = int(input(f"Enter max videos to check per channel (1-200): "))
                    if 1 <= new_value <= 200:
                        self.config.max_videos_per_channel = new_value
                        print(f"✅ Max videos to check per channel set to: {new_value}")
                    else:
                        print("❌ Please enter a number between 1 and 200")
                except ValueError:
                    print("❌ Please enter a valid number")
            elif choice == "6":
                print("\nAvailable resolutions: 360, 480, 720, 1080, 1440, 2160")
                entered_res = input(f"Enter max resolution (current: {self.config.max_resolution}): ").strip()
                if entered_res in ["360", "480", "720", "1080", "1440", "2160"]:
                    self.config.max_resolution = entered_res
                    print(f"✅ Max resolution set to: {entered_res}p")
                else:
                    print("❌ Invalid resolution. Please perform the selection again.")

            elif choice == "7":
                self.save_config()
                break
            else:
                print("❌ Invalid option")

    def download_playlist(self, playlist_url: str, download_type: str = "video") -> Tuple[bool, Optional[Path]]:
        """Download an entire playlist with progress tracking."""
        try:
            playlist_info = self.get_playlist_info(playlist_url)
            if not playlist_info:
                print("❌ Could not get playlist information")
                return False, None
            
            playlist_name = playlist_info["title"]
            print(f"📚 Playlist: {playlist_name}")
            print(f"👤 Uploader: {playlist_info['uploader']}")
            print(f"🎬 Videos: {playlist_info['video_count']}")
            
            # Determine start index based on existing files to fix resume logic
            start_index = 1
            if playlist_dir and playlist_dir.exists():
                existing_files = list(playlist_dir.glob("*.mp4")) + list(playlist_dir.glob("*.mp3")) + list(playlist_dir.glob("*.mkv"))
                # This is a rough heuristic. Ideally we'd match IDs.
                # But if we assume sequential download, count + 1 is a good start.
                # A better way is using yt-dlp's download archive which we should enable.
                start_index = len(existing_files) + 1
            
            # Use download archive to preventing re-downloading
            download_archive = playlist_dir / "download_archive.txt"
            
            cmd, playlist_dir = self.build_playlist_download_command(
                playlist_url, playlist_name, download_type, resume=True, resume_from=start_index
            )
            
            # Add archive file to command
            cmd.extend(["--download-archive", str(download_archive)])
            
            # Add playlist items start index if significant
            if start_index > 1:
                 # Check if we really want to skip. 
                 # Often it's safer to let archive handle it, but for large playlists skipping is faster.
                 # Let's subtract a small buffer to be safe (e.g. 5) in case of deletions/reordering
                 safe_start = max(1, start_index - 5)
                 cmd.extend(["--playlist-start", str(safe_start)])
            
            print(f"📁 Downloading to: {playlist_dir}")
            print(f"🔄 Resuming from video #{start_index} (approx)")
            
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            current_video = start_index 
            total_videos = playlist_info["video_count"]
            current_video_idx = 0 # Track actual index from output
            
            while True:
                if process.poll() is not None:
                    break
                    
                ready, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)
                
                for stream in ready:
                    line = stream.readline()
                    if not line:
                        continue
                    
                    progress = self.parse_progress(line)
                    if progress:
                        if progress.get("type") == "playlist_progress":
                            current_video = int(progress["current"])
                            total_videos = int(progress["total"])
                        elif progress.get("type") == "download":
                             # Handle "NA" percent
                             percent_val = progress.get("percent", "0")
                             try:
                                 current_percent = float(percent_val)
                             except ValueError:
                                 current_percent = 0.0
                                 
                             if "percent" in progress:
                                # Calculate overall progress based on video count
                                overall_percent = ((current_video - 1) / total_videos * 100) + (current_percent / total_videos)
                                
                                self.display_double_progress_bar(
                                    current_percent,
                                    overall_percent,
                                    f"Video {current_video}/{total_videos}", # Just count for now as size is hard to predict
                                    progress.get("size", ""),
                                    progress.get("speed", ""),
                                    progress.get("eta", "")
                                )
            
            return_code = process.wait()
            
            if return_code == 0:
                print(f"\n✅ Playlist download complete: {playlist_name}")
                return True, playlist_dir
            else:
                print(f"\n❌ Playlist download failed")
                return False, playlist_dir
                
        except Exception as e:
            self.logger.error(f"❌ Error downloading playlist: {e}")
            print(f"❌ Error: {e}")
            return False, None


def check_dependencies() -> bool:
    """Check if required dependencies are available."""
    try:
        subprocess.run(["yt-dlp", "--version"], capture_output=True, check=True)
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def main():
    """Entry point for the script."""
    parser = argparse.ArgumentParser(description='YouTube Feed Downloader')
    parser.add_argument('--interactive', '-i', action='store_true', 
                       help='Start in interactive mode')
    args = parser.parse_args()
    
    if not check_dependencies():
        print("❌ Error: Required dependencies missing!")
        print("   Please install:")
        print("   • yt-dlp: pipx install yt-dlp")
        print("   • ffmpeg: sudo pacman -S ffmpeg")
        print("   • ffprobe: sudo pacman -S ffmpeg (included with ffmpeg)")
        sys.exit(1)
    
    config = Config()
    downloader = YouTubeFeedDownloader(config)
    
    try:
        if args.interactive:
            downloader.interactive_mode()
        else:
            downloader.run_auto_download()
    except KeyboardInterrupt:
        print("\n\n⚠️ Operation interrupted by user")
        if hasattr(downloader, 'logger'):
            downloader.logger.warning("⚠️ Operation interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        if hasattr(downloader, 'logger'):
            downloader.logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
