#!/bin/bash

# yt-daily Installer Script
# Works on Arch, Debian/Ubuntu, Fedora

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}🚀 Starting yt-daily installation...${NC}"

# 1. Detect Package Manager and Install System Dependencies
echo -e "${BLUE}🔍 Checking for system dependencies...${NC}"

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

if command_exists pacman; then
    # Arch Linux
    TO_INSTALL=""
    command_exists yt-dlp || TO_INSTALL="$TO_INSTALL yt-dlp"
    command_exists ffmpeg || TO_INSTALL="$TO_INSTALL ffmpeg"
    python3 -c "import rich" &>/dev/null || TO_INSTALL="$TO_INSTALL python-rich"
    
    if [ -n "$TO_INSTALL" ]; then
        echo -e "${BLUE}📦 Installing missing packages via pacman:$TO_INSTALL...${NC}"
        sudo pacman -S --needed --noconfirm $TO_INSTALL
    else
        echo -e "${GREEN}✅ System dependencies already satisfied.${NC}"
    fi
elif command_exists apt-get; then
    # Debian/Ubuntu
    sudo apt-get update
    sudo apt-get install -y yt-dlp ffmpeg python3-rich
elif command_exists dnf; then
    # Fedora
    sudo dnf install -y yt-dlp ffmpeg python3-rich
else
    echo -e "${RED}❌ Multi-distro support: Package manager not recognized. Please install yt-dlp, ffmpeg, and python-rich manually.${NC}"
fi

# 2. Python Environment Check
# If rich isn't found, try to install it via pip with a bypass or venv (for Arch/other restricted distros)
if ! python3 -c "import rich" &>/dev/null; then
    echo -e "${BLUE}📦 System packages missing, trying pip...${NC}"
    pip3 install rich --upgrade --user --break-system-packages || pip3 install rich --upgrade --user || echo -e "${RED}⚠️ Could not install rich automatically. Please install it manually.${NC}"
fi

# 3. Create Application Directory
INSTALL_DIR="$HOME/.local/share/yt-daily"
BIN_DIR="$HOME/.local/bin"
echo -e "${BLUE}📂 Creating directories in $INSTALL_DIR...${NC}"
mkdir -p "$INSTALL_DIR"
mkdir -p "$BIN_DIR"

# 4. Copy the script
echo -e "${BLUE}📄 Copying YT_daily.py...${NC}"
cp YT_daily.py "$INSTALL_DIR/YT_daily.py"
chmod +x "$INSTALL_DIR/YT_daily.py"

# 5. Create a binary link/launcher
echo -e "${BLUE}🔨 Creating launcher in $BIN_DIR/Ytdaily...${NC}"
cat <<EOF > "$BIN_DIR/Ytdaily"
#!/bin/bash
python3 "$INSTALL_DIR/YT_daily.py" "\$@"
EOF
chmod +x "$BIN_DIR/Ytdaily"

# 6. Setup Shell Aliases
echo -e "${BLUE}🐚 Setting up shell aliases...${NC}"

# Bash
if [ -f "$HOME/.bashrc" ]; then
    if ! grep -q "alias Ytdaily=" "$HOME/.bashrc"; then
        echo "alias Ytdaily='$BIN_DIR/Ytdaily --interactive'" >> "$HOME/.bashrc"
    fi
fi

# Zsh
if [ -f "$HOME/.zshrc" ]; then
    if ! grep -q "alias Ytdaily=" "$HOME/.zshrc"; then
        echo "alias Ytdaily='$BIN_DIR/Ytdaily --interactive'" >> "$HOME/.zshrc"
    fi
fi

# Fish
if command -v fish >/dev/null; then
    mkdir -p "$HOME/.config/fish/functions"
    cat <<EOF > "$HOME/.config/fish/functions/Ytdaily.fish"
function Ytdaily
    $BIN_DIR/Ytdaily --interactive \$argv
end
EOF
fi

# 7. Setup Systemd Automation
echo -e "${BLUE}⏰ Setting up systemd daily auto-download...${NC}"
mkdir -p "$HOME/.config/systemd/user"

# Create Service
cat <<EOF > "$HOME/.config/systemd/user/ytdaily.service"
[Unit]
Description=yt-daily Feed Downloader
After=network-online.target

[Service]
Type=oneshot
ExecStart=$BIN_DIR/Ytdaily
Nice=19
IOSchedulingClass=idle

[Install]
WantedBy=default.target
EOF

# Create Timer
cat <<EOF > "$HOME/.config/systemd/user/ytdaily.timer"
[Unit]
Description=Run yt-daily once per day

[Timer]
OnBootSec=5min
Persistent=true
AccuracySec=1h

[Install]
WantedBy=timers.target
EOF

# Enable User Timer
systemctl --user daemon-reload
systemctl --user enable ytdaily.timer
systemctl --user start ytdaily.timer

echo -e "${GREEN}✅ Installation complete!${NC}"
echo -e "${GREEN}👉 You can now run 'Ytdaily' from your terminal.${NC}"
echo -e "${BLUE}Note: You might need to restart your terminal or run 'source ~/.bashrc' (or .zshrc) to use the alias immediately.${NC}"
