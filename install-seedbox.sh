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

# Can this account listen on address $1? (port 0 = any free port)
can_bind() {
    python3 -c 'import socket, sys; socket.socket().bind((sys.argv[1], int(sys.argv[2])))' "$1" "${2:-0}" 2>/dev/null
}

# Decides where the bridge listens (HOST) and the address Jellyfin should use (ADDR).
# Normally both are 127.0.0.1. Some providers run Jellyfin in its own container (its own
# network), where 127.0.0.1 is the container itself; then use the address the container
# reaches this machine at (its default gateway), or failing that this machine's public name.
pick_address() {
    HOST=127.0.0.1 ADDR=127.0.0.1 EXPOSED=no APP=
    me=$(id -u)
    pid=$(pgrep -u "$me" -x jellyfin 2>/dev/null | head -n 1)
    [ -n "$pid" ] || pid=$(pgrep -u "$me" -f 'jellyfin\.dll' 2>/dev/null | head -n 1)
    APP=Jellyfin
    if [ -z "$pid" ]; then
        pid=$(pgrep -u "$me" -f 'Plex Media Server' 2>/dev/null | head -n 1)
        APP=Plex
    fi
    [ -n "$pid" ] || { APP=; return 0; }      # not found: assume it shares this network
    their_ns=$(readlink "/proc/$pid/ns/net" 2>/dev/null)
    [ -n "$their_ns" ] || return 0            # can't tell: keep 127.0.0.1
    [ "$their_ns" = "$(readlink /proc/self/ns/net)" ] && return 0

    # Default gateway as Jellyfin sees it: /proc/net/route has it as little-endian hex.
    gw=$(awk '$2 == "00000000" { print $3; exit }' "/proc/$pid/net/route" 2>/dev/null)
    case "$gw" in
        [0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f][0-9A-Fa-f])
            # shellcheck disable=SC2046  # split into its four bytes, lowest first
            set -- $(echo "$gw" | sed 's/../& /g')
            gw=$((0x$4)).$((0x$3)).$((0x$2)).$((0x$1)) ;;
        *) gw= ;;
    esac
    if [ -n "$gw" ] && [ "$gw" != 0.0.0.0 ] && can_bind "$gw"; then
        HOST=$gw ADDR=$gw                     # only reachable from this machine and its containers
        return 0
    fi
    HOST=0.0.0.0 EXPOSED=yes
    ADDR=$(hostname -f 2>/dev/null)
    case "$ADDR" in *.*) ;; *) ADDR=$(hostname 2>/dev/null) ;; esac
    [ -n "$ADDR" ] || ADDR=YOUR-SEEDBOX-ADDRESS
}

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
    # shellcheck disable=SC2015  # any failed step means the download failed
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
        # shellcheck disable=SC2015  # any failed step means no Node.js
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

# 5. Where Jellyfin can reach the bridge
pick_address
# Address this machine can check the bridge on (0.0.0.0 listens on 127.0.0.1 too)
case "$HOST" in 0.0.0.0) CHECK=127.0.0.1 ;; *) CHECK=$HOST ;; esac
if [ "$HOST" != 127.0.0.1 ]; then
    say "$APP runs in its own container here, so it can't reach 127.0.0.1"
    if [ "$EXPOSED" = yes ]; then
        warn "Listening on every address and using $ADDR for $APP."
        warn "The streams can be reached from the internet, with no password, by anyone who"
        warn "knows the address and port."
    else
        echo "    Listening on $HOST, the address the container reaches this machine at."
        echo "    That isn't reachable from the internet."
    fi
fi

# 6. Start script (light settings: seedboxes have no GPU and share their CPU)
cat > "$START" <<EOF
#!/bin/sh
# Starts Spelunkr Bridge. Made by install-seedbox.sh - run that again to change the port
# or to pick the address again.
export PATH="$BIN:\$HOME/.local/bin:\$PATH"
cd "$DIR" || exit 1
# Is the port free? 0 = yes, 1 = taken, 2 = the address isn't there (yet).
# After a reboot the containers' network can come up after this runs, so wait for it.
n=0
while :; do
    python3 -c 'import errno, socket, sys
try: socket.socket().bind((sys.argv[1], int(sys.argv[2])))
except OSError as e: sys.exit(2 if e.errno == errno.EADDRNOTAVAIL else 1)' $HOST $PORT 2>/dev/null
    free=\$?
    [ "\$free" = 2 ] && [ \$n -lt 60 ] || break
    n=\$((n + 1)); sleep 5
done
if [ "\$free" = 2 ]; then
    echo "This machine has no address $HOST any more. Run install-seedbox.sh again to pick a new one."
    exit 1
elif [ "\$free" != 0 ]; then
    if pgrep -u "\$(id -u)" -f spelunkr2jellyfin.py >/dev/null 2>&1 \\
       || curl -fs -o /dev/null "http://$CHECK:$PORT/playlist.m3u"; then
        echo "Spelunkr Bridge is already running on port $PORT."
        exit 0
    fi
    echo "Port $PORT is taken by something else. Stop it, or run install-seedbox.sh again with another port."
    exit 1
fi
if [ -f channels.json ]; then
    python3 -m pip install --user -U -q yt-dlp 2>/dev/null \\
    || python3 -m pip install --user -U -q --break-system-packages yt-dlp 2>/dev/null
fi
exec python3 spelunkr2jellyfin.py --host $HOST --port $PORT --public-url http://$ADDR:$PORT \\
    --preset ultrafast --no-vhs-cards --log-file spelunkr.log
EOF
chmod +x "$START"

# 7. Start again after a reboot
if has crontab; then
    ( crontab -l 2>/dev/null | grep -v 'spelunkr-start.sh'; echo "@reboot $START >/dev/null 2>&1" ) | crontab - \
        || warn "couldn't set it to start after a reboot - run $START yourself after one"
else
    warn "no crontab here - after a reboot, start it again with: nohup $START >/dev/null 2>&1 &"
fi

# 8. (Re)start it and wait until the channels are loaded
say "Starting it"
RUNNING='spelunkr-start.sh|spelunkr2jellyfin.py'
pkill -u "$(id -u)" -f "$RUNNING" 2>/dev/null && sleep 2
can_bind "$HOST" "$PORT" \
    || die "port $PORT is taken by something else - run this again with a different port"
rm -f "$DIR/spelunkr.log"
nohup "$START" </dev/null >/dev/null 2>&1 &
[ "$YT" = yes ] && echo "    Loading the channels - the first start takes a few minutes while it reads the YouTube playlists..."
i=0
until curl -fs -o /dev/null "http://$CHECK:$PORT/playlist.m3u"; do
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
       http://$ADDR:$PORT/playlist.m3u
  2. TV Guide Data Providers > + > XMLTV. File or URL:
       http://$ADDR:$PORT/epg.xml
  3. Scheduled Tasks > Refresh Guide > Run

Plex instead (Live TV needs Plex Pass): Settings > Live TV & DVR > Set Up Plex DVR,
  enter the device address $ADDR:$PORT, and for the guide use the XMLTV address above.
EOF
if [ -z "$APP" ]; then
    cat <<EOF

Jellyfin (or Plex) wasn't running under your account, so this assumes it can reach
  127.0.0.1. If adding the tuner fails with "There was an error saving the TV provider",
  start Jellyfin and run this script again: it then checks which address to use.
EOF
fi
cat <<EOF

Log:    tail -f $DIR/spelunkr.log
Stop:   pkill -f spelunkr2jellyfin.py
Start:  nohup $START >/dev/null 2>&1 &
Update: run this script again
EOF
}

main "$@"
