#!/bin/sh
# Spelunkr Bridge - one-step install for a seedbox or any Linux box without Docker or root.
# Not for Windows (use the Windows zip) or Docker/Unraid (use the image).
#
#   curl -fsSL https://raw.githubusercontent.com/bwhiteman12-code/spelunkr-bridge/main/install-seedbox.sh | sh -s YOUR-PORT
#   or, after downloading it:  sh install-seedbox.sh YOUR-PORT
#
# YOUR-PORT is a port of your choosing (on a shared seedbox, one your provider assigned you).
# Run it again any time to update. Everything goes in your home folder.

DIR="$HOME/spelunkr-bridge"
BIN="$HOME/bin"
START="$HOME/spelunkr-start.sh"
REPO="https://github.com/bwhiteman12-code/spelunkr-bridge"

say()  { printf '\n==> %s\n' "$*"; }
warn() { printf '    ! %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*"; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

# Everything runs inside main, so a download cut off halfway through runs nothing.
main() {
PORT="$1"

case "$PORT" in
    ''|*[!0-9]*) die "add the port to use at the end, e.g.:  sh install-seedbox.sh 12345
       (on a shared seedbox, use a port your provider assigned you)" ;;
esac

mkdir -p "$BIN"
export PATH="$BIN:$HOME/.local/bin:$PATH"
has curl || die "curl is needed and isn't installed"

case "$(uname -m)" in
    x86_64|amd64)  FF_ARCH=amd64; NODE_ARCH=x64 ;;
    aarch64|arm64) FF_ARCH=arm64; NODE_ARCH=arm64 ;;
    *) FF_ARCH=; NODE_ARCH= ;;
esac

# 1. The bridge itself
say "Getting Spelunkr Bridge"
if [ -d "$DIR/.git" ] && has git; then
    git -C "$DIR" pull -q || warn "couldn't update - keeping the copy you have"
elif [ -d "$DIR" ]; then
    warn "$DIR already exists - keeping it"
elif has git; then
    git clone -q "$REPO.git" "$DIR" || die "download failed"
else
    mkdir -p "$DIR" && curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$DIR" --strip-components=1 \
        || die "download failed"
fi

# 2. Python 3.8+
has python3 || die "python3 isn't installed - ask your seedbox provider"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || die "python3 is too old (3.8 or newer is needed)"

# 3. ffmpeg + ffprobe
if has ffmpeg && has ffprobe; then
    say "ffmpeg: found"
else
    say "Installing ffmpeg in $BIN"
    [ -n "$FF_ARCH" ] || die "no ffmpeg build for $(uname -m) - install ffmpeg yourself"
    curl -fL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-$FF_ARCH-static.tar.xz" \
        | tar xJ -C "$BIN" --strip-components=1 --wildcards '*/ffmpeg' '*/ffprobe' \
        || die "ffmpeg download failed"
fi

# 4. yt-dlp + Node.js for the YouTube channels (optional - the Spelunkr channels work without them)
pip_install() {
    python3 -m pip install --user -U -q "$@" 2>/dev/null \
    || python3 -m pip install --user -U -q --break-system-packages "$@" 2>/dev/null
}
say "Installing yt-dlp (for the YouTube channels)"
if ! python3 -m pip --version >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$DIR/get-pip.py" \
        && { python3 "$DIR/get-pip.py" --user -q 2>/dev/null \
             || python3 "$DIR/get-pip.py" --user -q --break-system-packages 2>/dev/null; }
    rm -f "$DIR/get-pip.py"
fi
YT=no
if pip_install yt-dlp && python3 -m yt_dlp --version >/dev/null 2>&1; then
    YT=yes
else
    warn "couldn't install yt-dlp - only the Spelunkr channels will be set up"
fi

if [ "$YT" = yes ] && ! has node; then
    say "Installing Node.js in $HOME/node"
    if [ -n "$NODE_ARCH" ]; then
        NODE_URL="https://nodejs.org/dist/latest-v22.x"
        F=$(curl -fsSL "$NODE_URL/SHASUMS256.txt" | grep -o "node-v[0-9.]*-linux-$NODE_ARCH.tar.xz" | head -1)
        mkdir -p "$HOME/node" \
            && [ -n "$F" ] && curl -fsSL "$NODE_URL/$F" | tar xJ -C "$HOME/node" --strip-components=1 \
            && ln -sf "$HOME/node/bin/node" "$BIN/node" \
            || warn "couldn't install Node.js - some YouTube videos may not play"
    else
        warn "no Node.js build for $(uname -m) - some YouTube videos may not play"
    fi
fi

if [ "$YT" = yes ] && [ ! -f "$DIR/channels.json" ]; then
    cp "$DIR/examples/channels.json" "$DIR/channels.json" && say "Added the example YouTube channels"
fi

# 5. Start script (light settings: seedboxes have no GPU and share their CPU)
cat > "$START" <<EOF
#!/bin/sh
# Starts Spelunkr Bridge. Made by install-seedbox.sh - run that again to change the port.
export PATH="$BIN:\$HOME/.local/bin:\$PATH"
cd "$DIR" || exit 1
if [ -f channels.json ]; then
    python3 -m pip install --user -U -q yt-dlp 2>/dev/null \\
    || python3 -m pip install --user -U -q --break-system-packages yt-dlp 2>/dev/null
fi
exec python3 spelunkr2jellyfin.py --host 127.0.0.1 --port $PORT --public-url http://127.0.0.1:$PORT \\
    --preset ultrafast --no-vhs-cards --log-file spelunkr.log
EOF
chmod +x "$START"

# 6. Start again after a reboot
if has crontab; then
    ( crontab -l 2>/dev/null | grep -v 'spelunkr-start.sh'; echo "@reboot $START >/dev/null 2>&1" ) | crontab - \
        || warn "couldn't set it to start after a reboot - run $START yourself after one"
else
    warn "no crontab here - after a reboot, start it again with: nohup $START >/dev/null 2>&1 &"
fi

# 7. (Re)start it and wait until the channels are loaded
say "Starting it"
RUNNING='spelunkr-start.sh|spelunkr2jellyfin.py'
pkill -u "$(id -u)" -f "$RUNNING" 2>/dev/null && sleep 2
python3 -c 'import socket, sys; socket.socket().bind(("127.0.0.1", int(sys.argv[1])))' "$PORT" 2>/dev/null \
    || die "port $PORT is taken by something else - run this again with a different port"
rm -f "$DIR/spelunkr.log"
nohup "$START" </dev/null >/dev/null 2>&1 &
[ "$YT" = yes ] && echo "    Loading the channels - the first start takes a few minutes while it reads the YouTube playlists..."
i=0
until curl -fs -o /dev/null "http://127.0.0.1:$PORT/playlist.m3u"; do
    i=$((i + 1))
    if ! pgrep -u "$(id -u)" -f "$RUNNING" >/dev/null 2>&1; then
        tail -15 "$DIR/spelunkr.log" 2>/dev/null
        die "the bridge stopped. To see why, run it in this window:  $START"
    fi
    [ $i -gt 200 ] && die "still not ready after 10 minutes - see $DIR/spelunkr.log"
    sleep 3
done

cat <<EOF

==> Done! Spelunkr Bridge is running on port $PORT.

In Jellyfin: Dashboard > Live TV
  1. Tuner Devices > + > M3U Tuner. File or URL:
       http://127.0.0.1:$PORT/playlist.m3u
  2. TV Guide Data Providers > + > XMLTV. File or URL:
       http://127.0.0.1:$PORT/epg.xml
  3. Scheduled Tasks > Refresh Guide > Run

Plex instead (Live TV needs Plex Pass): Settings > Live TV & DVR > Set Up Plex DVR,
  enter the device address 127.0.0.1:$PORT, and for the guide use the XMLTV address above.

Log:    tail -f $DIR/spelunkr.log
Stop:   pkill -f spelunkr2jellyfin.py
Start:  nohup $START >/dev/null 2>&1 &
Update: run this script again
EOF
}

main "$@"
