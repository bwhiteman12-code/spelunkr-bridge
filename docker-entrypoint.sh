#!/bin/sh
# Starts the bridge from the container's settings.
#   PUBLIC_URL    address Jellyfin uses to reach this container, e.g. http://192.168.1.20:8765
#   GPU           render node for GPU encoding, e.g. /dev/dri/renderD128 (empty: try each one)
#   UPDATE_YTDLP  true: update yt-dlp at every start (YouTube changes often)
#   EXTRA_ARGS    any other options, e.g. "--skip vacation-slides --no-vhs-cards"
# Out of the box it serves spelunkr.tv's channels. To add your own YouTube channels, put a
# channels.json in /config (start from /app/examples). Anything after the image name
# (Unraid "Post Arguments") is passed on as options too.

if [ "$UPDATE_YTDLP" = "true" ]; then
    pip install -q --no-cache-dir --disable-pip-version-check --root-user-action=ignore -U "yt-dlp[default]" \
        || echo "couldn't update yt-dlp - using the one in the image"
fi

mkdir -p /config/cache
if [ -f /config/channels.json ]; then
    echo "custom channels: /config/channels.json"
else
    echo "no /config/channels.json - serving spelunkr.tv's channels only (examples to start from: /app/examples)"
fi

set -- --port 8765 --cache /config/cache --channels /config/channels.json "$@"
[ -n "$PUBLIC_URL" ] && set -- "$@" --public-url "$PUBLIC_URL"
[ -n "$GPU" ] && set -- "$@" --gpu-device "$GPU"

# EXTRA_ARGS is split on spaces on purpose
# shellcheck disable=SC2086
exec python -u /app/spelunkr2jellyfin.py "$@" $EXTRA_ARGS
