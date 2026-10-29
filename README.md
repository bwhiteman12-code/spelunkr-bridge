# Spelunkr → Jellyfin Live TV

Turns the 30 channels on [spelunkr.tv](https://spelunkr.tv/) into live TV channels with a full TV guide in Jellyfin. The channels run in sync with the website: the same show, bump card and commercial plays at the same moment as on spelunkr.tv.

## Install

**Windows, with Jellyfin on your own PC:**
1. Download `Spelunkr-Bridge-Windows.zip` from the [latest release](../../releases/latest).
2. Unzip it somewhere permanent, e.g. `C:\Spelunkr Bridge`.
3. Double-click `install-windows.bat`. When it's ready, it shows the two URLs.
4. In Jellyfin, go to Dashboard → Live TV:
   - add an **M3U Tuner** with `http://localhost:8765/playlist.m3u`
   - add an **XMLTV** guide with `http://localhost:8765/epg.xml`
   - click **Refresh Guide Data**

Details: [Setup on Windows](#setup-on-windows-jellyfin-on-your-own-pc).

**Unraid / Docker:**
1. Run the image `ghcr.io/bwhiteman12-code/spelunkr-bridge:latest`:
   - publish port 8765
   - mount a folder at `/config`
   - set `PUBLIC_URL=http://YOUR-SERVER-IP:8765`

   Unraid users can install it from the template in this repo; see [Setup on Unraid / Docker](#setup-on-unraid--docker).
2. In Jellyfin, add an **M3U Tuner** with `http://YOUR-SERVER-IP:8765/playlist.m3u` and an **XMLTV** guide with `http://YOUR-SERVER-IP:8765/epg.xml`. Then click **Refresh Guide Data**.

*A fan project, not affiliated with spelunkr.tv, Everything Is Terrible! or RedLetterMedia.*

## How it works

Spelunkr doesn't host video streams. The website downloads a schedule (`lineup.json` plus one playlist per channel) and runs a scheduler in your browser. That scheduler decides what plays when: the shows, the text "bump" cards and music between them, the vintage commercial breaks, and the "Up Next" cards. The videos themselves play straight from the Internet Archive.

`spelunkr2jellyfin.py` includes a Python copy of that scheduler. It was checked against the site's own code: all 30 channels matched to the millisecond at three different times (now, 10 days ahead, and 4 days back). For each viewer, the script turns the schedule into one continuous live stream:

| URL | What it is |
|---|---|
| `http://<server>:8765/playlist.m3u` | Channel list → Jellyfin **M3U Tuner** |
| `http://<server>:8765/epg.xml` | 72-hour TV guide → Jellyfin **XMLTV** |
| `http://<server>:8765/stream/<channel>.ts` | The live stream (ffmpeg runs only while someone is watching) |
| `http://<server>:8765/` | Status page showing what's on right now |
| `http://<server>:8765/now.json` | The same information as JSON |

What you'll see:
- **Shows** from archive.org. The "Rewind" channels are whole off-air tapes with their original commercials.
- **Bump cards:** the channel's slides in its own color on black, then Spelunkr's gold-on-navy slides. Each card has the same music as the site, with fades and on-screen music credits.
- **Commercial breaks** on the channels that have them: the channel's card, a vintage commercial from Spelunkr's ad reels picked to match the era of the show, then the Spelunkr card.
- **Up Next cards** on the block-programmed channels (Homeroom, Fallout Shelter, Rubber Hose, Unpaid Programming), with their music stings and the real start times of the next two shows.
- In the guide, each show's slot runs until the next show starts, so the bumps and breaks after it count as part of it.

The script re-checks the lineup every 30 minutes. Everything it downloads is saved in a `cache` folder next to the script, so if Spelunkr's server is down when the container restarts, it starts from the last saved schedule.

**Fonts:** the cards use the same retro Star4000 fonts as the site, converted to `.ttf`. Put `star4000.ttf` and `star4000-large.ttf` next to the script. Without them, the cards fall back to DejaVu Sans. Text the retro font can't draw, such as Japanese, uses DejaVu automatically.

**Not copied from the site:** Vacation Slides plays background music over its slideshows on the website. Here it plays the slides' own audio.

## On-air touches

- **Tune-in static:** when a channel starts up, you get a few seconds of black-and-white TV snow with a green on-screen "CH 300 / HACK FRAUD TV" readout and "ADJUSTING THE ANTENNA...". This plays while the first tape loads, and the show starts right where the snow ends. Turn it off with `--no-static`.
- **Corner logo:** each channel's pixel-art logo shows faintly in the bottom-right corner during shows. It's hidden during commercials and cards, like real broadcast TV. Turn it off with `--no-logo-bug`.
- **Worn-VHS look:** bump cards on every channel look like a worn VHS dub, so they match the tapes around them:
  - soft, smeared detail and color bleed
  - milky blacks and tape grain
  - head-switching noise along the bottom edge
  - a tracking glitch that rolls through for under a second about every 41 seconds

  The example Hack Fraud TV channel gives its episodes the same treatment, because they're shot digitally and would otherwise look too clean. The filter runs on the CPU at SD size, so it's light even when encoding on the GPU. Turn it off for cards with `--no-vhs-cards`. In `channels.json`, `"vhs": ["shows"]` adds it to a channel's shows, and `"ads"` adds it to its commercials.
- **Shared streams:** every TV watching the same channel shares one stream and one encode, so 4 TVs on one channel cost the same as 1. A TV that tunes in later joins at the live point. A channel stays on the air for 30 seconds after its last viewer leaves, so flipping away and back is instant.
- **GPU encoding:** with `/dev/dri` passed into the container, the bridge encodes on an Intel GPU through Quick Sync (or VAAPI) instead of the CPU. At startup it runs a 1-second test encode and logs `encoding on the GPU: Quick Sync on /dev/dri/renderD129`. If the GPU doesn't work, it logs why and uses the CPU. Use `--gpu-device` to pick the same render node Jellyfin uses.
- **Episode descriptions:** YouTube episodes get their real descriptions in the guide (just the first paragraph, without the links and plugs). These are fetched in the background a batch at a time after the first start, and saved.
- **Unplayable videos:** some YouTube videos are age-restricted and can't play without signing in. The background description pass checks every video and drops these from the lineup. If one is ever found while airing, the channel moves to the next show within 30 seconds. Otherwise the change takes effect at the next show. Everything after the dropped video moves up, so Jellyfin's guide catches up at its next Refresh Guide.

### Turning the extras off

The TV static, the corner logo and the VHS-style bump cards are on for every channel by default. Each has an option to turn it off:

| Option | Turns off |
|---|---|
| `--no-static` | the TV snow while a channel tunes in |
| `--no-logo-bug` | the channel logo in the corner during shows |
| `--no-vhs-cards` | the worn-VHS look on bump cards (they're drawn clean) |

**Windows:**
1. Open `options.txt` in the Spelunkr Bridge folder with Notepad.
2. Remove the first `#` in front of each option you want, then save. For example:
   ```
   --no-static              # no TV snow while a channel tunes in
   --no-logo-bug            # no channel logo in the corner during shows
   # --no-vhs-cards           # bump cards drawn clean instead of as worn VHS
   ```
   Here the static and the corner logo are turned off, and the VHS cards stay on.
3. Run `install-windows.bat` again to restart it with the new options.

**Unraid:** Docker tab → click the container → **Edit** → set **Extra options** to e.g. `--no-static --no-logo-bug`. On a hand-made container, add a Variable `EXTRA_ARGS` instead. Then Apply.

**Docker Compose:** set `EXTRA_ARGS=--no-static --no-logo-bug` under `environment:` in `docker-compose.yml`, then run `docker compose up -d`.

The same works for any other [option](#options), e.g. `--skip vacation-slides` to leave a channel out.

## Your own channels (channels.json)

Out of the box you get spelunkr.tv's channels. You can add your own channels built from YouTube playlists. They run on the same scheduler as Spelunkr's, with bump cards, music, commercial breaks and a guide. They're defined in a file called `channels.json`:

- **Windows:** next to `spelunkr2jellyfin.py`. Run `install-windows.bat` again afterwards, so it can set up Node.js.
- **Docker / Unraid:** in the `/config` folder, e.g. `/mnt/user/appdata/spelunkr/channels.json`. Restart the container afterwards.

### Make your own channel

1. Copy [`examples/starter-channels.json`](examples/starter-channels.json) to where it goes (above) and rename it `channels.json`.
2. Change these:
   - `id`: a short name with no spaces, used in URLs
   - `name`: what Jellyfin shows
   - `dial`: the channel number (Spelunkr's own channels use 1–99)
   - `color`: the card and logo color
   - the `playlist` URL: any public YouTube playlist
3. Write your own `bumps`: the cards between shows. Each card is a few short `slides` shown one after another. `network_bumps` are the station-ID cards that follow them.
4. Restart. Check the log for `lineup loaded: … (1 custom)` and the channel shows up in Jellyfin after a **Refresh Guide Data**.

Want more than one show on a channel? Replace `"playlist"` with a `"shows"` list, each with its own `name`, `playlists` and `order`. To add another channel, add another entry to `"channels"`. JSON is picky about commas, so if a channel doesn't show up, the log says what's wrong. The full set of settings is listed under [All settings](#all-settings).

### The example channels

[`examples/channels.json`](examples/channels.json) holds two complete channels that use most of the settings. To run them as they are, copy that file to where it goes (above).

**Hack Fraud TV** (channel 300) is a RedLetterMedia fan channel that isn't affiliated with RedLetterMedia. It mixes three shows from their YouTube playlists:
- **Best of the Worst:** 165 episodes, oldest first.
- **re:View:** 144 episodes, oldest first.
- **Mr. Plinkett Reviews:** the Star Wars prequel reviews, then the other reviews oldest first.

Each show keeps its own order, and the shows are spread evenly through the rotation like a TV schedule. The full rotation is about 300 hours before it repeats. Older reviews uploaded in parts ("Part 1 of 7") air back-to-back as one program, with commercial breaks between parts, and show as one listing in the guide. To add a show, add another entry under `"shows"` with a name and one or more playlists.

- **Episodes** stream straight from YouTube through yt-dlp, with nothing downloaded and no account needed. The playlist is re-read every 6 hours. When it changes (a new episode), the new lineup takes over at the start of the next show, the same way Spelunkr handles playlist changes.
- **Commercial breaks** use 80s and 90s US commercials from Spelunkr's ad reels, with movie trailers and VHS previews weighted 3×. They come between episodes (after 20+ minutes), and also mid-episode wherever SponsorBlock users have marked the show's segment-change title stings: about a third of episodes, roughly 100 extra breaks per full run. The guide still shows each episode as one listing.
- **Bump cards** are Hack Fraud TV's own, in Spelunkr's style. Each show has its own set (24 Best of the Worst, 12 re:View, 12 Plinkett) plus a few general ones. A break draws from the cards of the show that just aired. To tag a card for a show, give it `"show": "re:View"`; untagged cards can appear anywhere.
- **Music:** 34 tracks in the `music` folder, plus 22 from Spelunkr's own music library, which stream from Spelunkr's server like they do on its channels. Each channel draws on a different set of Spelunkr's tracks: synthwave and sports themes on Hack Fraud TV, infomercial and kids' TV music on EISTV. Both share some of Spelunkr's network music for station IDs. To use one of Spelunkr's tracks in your own channel, give it a `"filename"` from Spelunkr's library instead of a `"file"`. The bundled tracks are trimmed to 100 seconds and leveled to the same loudness. There are 11 by Kevin MacLeod (incompetech.com), plus synthwave, vaporwave, chiptune and smooth jazz from the Internet Archive: Lazerhawk, GUNSHIP, AIRGLOW, Timecrawler 82, Telematix, 2003 Toyota Corolla, Thought Pyramid, Dr. Tuxedo and others. Every track is Creative Commons or public domain, and each is credited on screen while it plays.
- **Picture:** 4:3 at 640×480 with the widescreen episodes letterboxed. To fill the screen instead, like VHS pan & scan, change `"fit": "letterbox"` to `"fit": "crop"`. For widescreen, add `"size": "854x480"`.

**EISTV** (Everything Is Terrible TV, channel 301) is a fan channel built from all 43 of [Everything Is Terrible!](https://www.youtube.com/@EVERYTHINGISTERRIBLE/playlists)'s YouTube playlists. It isn't affiliated with Everything Is Terrible!.
- **Themed blocks:** the clips are short, so each playlist airs as half-hour blocks of back-to-back clips: a block of PUPPETS!, then COPAGANDA, and so on. Each block is one guide listing, with the clip titles in its description. The themes rotate through the day.
- **Each clip airs once:** about 900 unique clips (36 hours) before the rotation repeats. A clip that's on several playlists airs under its most specific one. Playlists under 10 minutes fold into a bigger theme their clips also belong to.
- **Commercials** come after 20+ minutes, between clips. There are no SponsorBlock splits.
- **Bump cards** are about the art collective, the Jerry Maguire VHS drive (P.O. Box 1996, Nowhere, USA 00000) and "call now" offers. Every phone number is in the 555-01xx range set aside for fiction.
- Its logo is a pixel-art stained-glass wheel with an all-seeing VHS tape.

### All settings

Per channel:
- `id`, `name`, `dial`, `color`, `description`.
- `fit`: `"letterbox"` or `"crop"`. `size`: e.g. `"854x480"` for widescreen.
- `bumps` and `network_bumps`: cards made of `slides`. Add `"show": "Name"` to a card to show it only after that show.
- `music` and `network_music`: the music beds under the cards. Each track has a `file` (an mp3; paths like `music/...` also find the bundled music folder), `title`, `artist`, `source`, `license` and `license_version`. The credit is shown on screen.
- `vhs`: `["shows"]` and/or `["ads"]` adds the worn-VHS look to that channel's shows or commercials.
- `logomark`: a 32×32 pixel grid in Spelunkr's format. An optional `palette` adds your own letter → color pairs.
- `ads`: years, exclude and boost filters, boost weight.
- `mid_break_minutes` (default 8) and `ad_gap_minutes` (default 20).
- `card_seconds`: how long each bump card stays up. The examples use 20. The slides slow down evenly to fill the time, and a full station break (channel card plus network card) runs about 44 seconds. Leave it out for Spelunkr's own pacing.

Under `"youtube"`:
- `"playlist"` (one show) or `"shows"`: a list of `{"name", "playlists", "order"}`. `order` is `"oldest-first"` or the playlist's own order.
- `"breaks": "sponsorblock"` also puts commercial breaks mid-episode, where SponsorBlock users marked segment changes.
- `"unique": true` airs each video once.
- `"min_show_minutes"` (default 10) sets the size below which a playlist folds into a bigger one.
- `"block_minutes"` groups short clips into blocks.
- `"stagger": true` spreads many shows around the rotation.
- `"title_strip"` is a regex removed from every video title.

Edits take effect at the next lineup refresh (every 30 minutes) or on restart.

The first start with a new channel takes a little longer while it reads the playlists: about 2 extra minutes for EISTV's 43. After that, episode descriptions are fetched in the background, which also finds and drops any age-restricted clips.

Your own channels use `yt-dlp` and Node.js. The Docker image includes both, and the Windows installer sets them up once a `channels.json` exists. The container updates yt-dlp at every start, which matters because YouTube changes often and old versions stop working. Note: streaming YouTube this way isn't permitted by YouTube's terms. It's fine for home tinkering, but keep it personal.

## Setup on Unraid / Docker

There's a ready-made image, `ghcr.io/bwhiteman12-code/spelunkr-bridge:latest`, built from this repo for both x86 and ARM. It already contains ffmpeg, yt-dlp, Node.js, the Intel GPU drivers, the fonts, the music and the example channels (in `/app/examples`).

**Unraid, with the template:**
1. Docker tab → **Template repositories** (at the bottom of the page). Add `https://github.com/bwhiteman12-code/spelunkr-bridge` and click Save.
2. **Add Container** → Template → pick **spelunkr-bridge**.
3. Set **Public URL** to `http://YOUR-UNRAID-IP:8765`.
4. **Intel GPU:** keep the `/dev/dri` device and set **GPU render node** to the one Jellyfin uses (Dashboard → Playback → Transcoding → QSV Device). No Intel GPU? Remove the device and it uses the CPU.
5. Apply. Check the log for `lineup loaded`, then open `http://YOUR-UNRAID-IP:8765/`.

**Unraid by hand:** Docker tab → Add Container with these settings:
- Repository: `ghcr.io/bwhiteman12-code/spelunkr-bridge:latest`, Network: Bridge
- Port: `8765` → `8765`
- Path: `/config` → `/mnt/user/appdata/spelunkr`
- Variable `PUBLIC_URL`: `http://YOUR-UNRAID-IP:8765`
- Optional: Device `/dev/dri` and Variable `GPU`: `/dev/dri/renderD128`

**Compose (Compose Manager plugin or any Docker host):** use [`docker-compose.yml`](docker-compose.yml). Set `PUBLIC_URL` and `TZ`, then run `docker compose up -d`.

**Settings:**

| Variable | What it does |
|---|---|
| `PUBLIC_URL` | How Jellyfin reaches the container, e.g. `http://192.168.1.20:8765` |
| `GPU` | GPU render node, e.g. `/dev/dri/renderD128`. Empty tries each one. |
| `EXTRA_ARGS` | Any other [options](#options), e.g. `--skip vacation-slides --no-vhs-cards` |
| `UPDATE_YTDLP` | `true` (default) updates yt-dlp at every start |
| `TZ` | Your timezone, e.g. `America/Chicago` (Unraid sets this itself) |

The `/config` folder holds the saved schedules. To change the custom channels, copy [`channels.json`](channels.json) into `/config`, edit it, and restart. Music paths like `music/...` still find the built-in music; to use your own, put a `music` folder in `/config`.

**Updating:** Docker tab → **Check for Updates** → apply, or `docker compose pull && docker compose up -d`.

**Moving from the old `python:3.12-alpine` + Post Arguments setup:**
1. Change the Repository to the image above.
2. Clear Post Arguments.
3. Change the container path `/app` to `/config`, keeping the same appdata folder.
4. Add `PUBLIC_URL` and `GPU` as variables.

Your existing `channels.json`, music and saved schedules carry over.

## Setup on Windows (Jellyfin on your own PC)

1. Put this folder somewhere permanent, e.g. `C:\Spelunkr Bridge`. It must contain `spelunkr2jellyfin.py`, the two `star4000` fonts, the `music` folder and the `install-windows` files.
2. Double-click **`install-windows.bat`**. It will:
   - find Python 3, or offer to install it with winget
   - install or update yt-dlp, which the YouTube channels need
   - offer to install Node.js if you've added your own channels (YouTube needs it to unlock video streams)
   - find the ffmpeg that comes with Jellyfin (no separate install needed)
   - start the bridge in the background with no window, and set it to start again every time you log in
   - wait until the channels are loaded, then show the two URLs for Jellyfin

   With your own YouTube channels added, the first start takes a few minutes while it reads the playlists.
3. Add it to Jellyfin as below, using `http://localhost:8765/playlist.m3u` and `http://localhost:8765/epg.xml`.

It encodes on your graphics card when it can: NVIDIA (NVENC), then Intel (Quick Sync), then AMD (AMF), otherwise the CPU. The log is `spelunkr.log` in the folder. What's on now: `http://localhost:8765/`.

- **Only reachable from this PC:** the bridge listens on this PC only, so Windows Firewall never asks. TVs and phones still work, because Jellyfin relays the streams to them. To open the status page from other devices, run `install-windows.bat -Lan` and allow it through the firewall.
- **Stop or remove it:** `install-windows.bat -Uninstall`.
- **Update the script:** replace the files and run `install-windows.bat` again. It restarts the bridge.
- **See the log live:** run `install-windows.bat -Uninstall`, then run `start-spelunkr.bat`, which runs the bridge in a window.
- It only runs while you're logged in, and only while the PC is awake, just like Jellyfin's tray app.

## Adding it to Jellyfin

Dashboard → Live TV:
1. **Tuner Devices → + → M3U Tuner.** File or URL: `http://YOUR-UNRAID-IP:8765/playlist.m3u`
2. **TV Guide Data Providers → + → XMLTV.** File or URL: `http://YOUR-UNRAID-IP:8765/epg.xml`. The default categories already include movie, kids and sports. If you have other live TV, you can limit this guide to the Spelunkr tuner.
3. **Scheduled Tasks → Refresh Guide → ▶**

## Options

```
python spelunkr2jellyfin.py --help
  --port 8765               port to listen on
  --public-url URL          address Jellyfin uses to reach this server
  --size 640x480            output resolution (most tapes are 4:3 SD)
  --maxrate 2000            max video kbps
  --preset veryfast         x264 speed/quality (ultrafast = lower CPU)
  --hours 72                hours of guide to publish
  --lead 0                  run N seconds ahead of the website, to cancel out Jellyfin's buffering
  --hwaccel auto            GPU encoding: auto (Linux: Quick Sync, then VAAPI; Windows: NVIDIA, Quick Sync,
                            then AMD; then CPU), qsv, vaapi, nvenc, amf or none
  --gpu-device auto         Linux GPU render node, e.g. /dev/dri/renderD129 (auto: try each one)
  --host 0.0.0.0            address to listen on (127.0.0.1 = this PC only)
  --ffmpeg / --ffprobe      ffmpeg to use (default: on PATH, else Jellyfin's copy on Windows)
  --log-file FILE           also write the log to a file
  --no-static               skip the tune-in TV snow
  --no-logo-bug             skip the corner channel logo
  --no-vhs-cards            draw bump cards clean instead of as worn VHS
  --only suplex,primetime   only these channels
  --skip vacation-slides    leave channels out
  --font FILE               font for bump cards
  --cache DIR               where to save schedule files (default ./cache)
  --now [--at MS --dump N]  print what's on each channel and exit (for testing)
  --export DIR              write playlist.m3u + epg.xml as files and exit
```

## When Spelunkr changes things

- **New channels, new playlists, new bumps, new commercials:** these show up automatically.
- **A change to the scheduler itself:** Spelunkr marks each playlist with a scheduler version (currently 5). If a newer version appears, the log warns `uses scheduler engine N, which this script doesn't know yet`. The channels keep working, but timing may drift from the website until the script is updated.
- **Data files move:** if Spelunkr moves their schedule files, update `BROADCAST` at the top of the script.

## Notes

- **Staying in sync:** the bridge starts each stream a few seconds ahead to cover ffmpeg's startup time, which it learns as it goes. It also starts loading each next segment before the current one ends. If the first picture still arrives late, it catches up at the next hand-off. What's left is Jellyfin's own buffering, usually a few seconds. If Jellyfin consistently runs N seconds behind spelunkr.tv, add `--lead N` to the command.
- Changing channels takes about 3–10 seconds while ffmpeg opens the tape on archive.org. Long tapes take longest, because their index alone can be tens of megabytes.
- If archive.org can't load a tape, you'll see color bars for up to 20 seconds, then it moves on.
- All the video comes from the Internet Archive, and the bump music comes from Spelunkr's audio server. If you enjoy this, consider [donating to the Archive](https://archive.org/donate) or [tipping Spelunkr](https://ko-fi.com/spelunkrtv).
