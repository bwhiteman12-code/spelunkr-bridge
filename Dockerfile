# Spelunkr Bridge - spelunkr.tv channels as live TV for Jellyfin
FROM python:3.12-alpine

# ffmpeg + a fallback font; nodejs lets yt-dlp unlock YouTube streams.
# The Intel media driver (x86 only) is for Quick Sync / VAAPI encoding; without it the CPU is used.
RUN apk add --no-cache ffmpeg font-dejavu tzdata nodejs \
 && if [ "$(uname -m)" = "x86_64" ]; then \
      apk add --no-cache intel-media-driver libvpl-gpu-rt \
      || apk add --no-cache intel-media-driver onevpl-intel-gpu \
      || apk add --no-cache intel-media-driver \
      || true; \
    fi \
 && pip install --no-cache-dir --disable-pip-version-check --root-user-action=ignore "yt-dlp[default]"

WORKDIR /app
COPY spelunkr2jellyfin.py channels.json star4000.ttf star4000-large.ttf docker-entrypoint.sh ./
COPY music ./music
RUN chmod +x docker-entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PUBLIC_URL="" \
    GPU="" \
    UPDATE_YTDLP=true \
    EXTRA_ARGS=""
VOLUME /config
EXPOSE 8765
ENTRYPOINT ["/app/docker-entrypoint.sh"]
