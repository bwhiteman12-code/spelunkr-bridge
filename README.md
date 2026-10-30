<img src="assets/icon.png" alt="" width="96" align="right">

# Spelunkr → Jellyfin & Plex Live TV

Turns every channel on [spelunkr.tv](https://spelunkr.tv/) into a live TV channel with a full TV guide in **Jellyfin** or **Plex**. The channels run in sync with the website: the same show, bump card and commercial plays at the same moment as on spelunkr.tv.

You always get all of Spelunkr's channels, including ones added after you install. The bridge reads Spelunkr's current channel list when it starts and checks it again every 30 minutes, so new channels appear without updating anything.

| | Jellyfin | Plex |
|---|---|---|
| Cost | Free | Live TV needs a paid **Plex Pass** subscription |
| How it's added | M3U tuner + XMLTV guide | As an HDHomeRun tuner + XMLTV guide (no Threadfin or xTeVe needed) |

## Install

Two steps: run the bridge, then add it to Jellyfin or Plex.

### 1. Run the bridge

**Windows** (on the same PC as Jellyfin or Plex):
1. Download `Spelunkr-Bridge-Windows.zip` from the [latest release](../../releases/latest).
2. Unzip it somewhere permanent, e.g. `C:\Spelunkr Bridge`.
3. **Plex users only:** install ffmpeg once by running `winget install Gyan.FFmpeg` in a terminal. Jellyfin already comes with it.
4. Double-click `install-windows.bat`. When it's ready, it shows the addresses to use.

Details: [Setup on Windows](#setup-on-windows-jellyfin-on-your-own-pc).

**Unraid / Docker:** run the image `ghcr.io/bwhiteman12-code/spelunkr-bridge:latest`:
- publish port 8765
- mount a folder at `/config`
- set `PUBLIC_URL=http://YOUR-SERVER-IP:8765`

Unraid users can install it from the template in this repo; see [Setup on Unraid / Docker](#setup-on-unraid--docker).

**Seedbox, or Linux without Docker:** for a Linux machine where you can't use Docker or root, such as a shared seedbox. Windows and Docker users don't need this; use the steps above. Pick a port (on a shared seedbox, one your provider assigned you) and run this over SSH, with your port in place of `12345`:
```bash
curl -fsSL https://raw.githubusercontent.com/bwhiteman12-code/spelunkr-bridge/main/install-seedbox.sh | sh -s 12345
```
Details: [Setup on a seedbox](#setup-on-a-seedbox-linux-without-docker).

### 2. Add it to Jellyfin or Plex

In the addresses below, replace `SERVER:8765` with where the bridge runs:
- **Windows:** `127.0.0.1:8765` (this PC)
- **Docker:** your server's IP address, e.g. `192.168.1.20:8765`
- **Seedbox:** the address the installer shows at the end, usually `127.0.0.1` with your port, e.g. `127.0.0.1:12345`

**Jellyfin** (free). Go to Dashboard → Live TV:
1. Add an **M3U Tuner** with `http://SERVER:8765/playlist.m3u`.
2. Add an **XMLTV** guide with `http://SERVER:8765/epg.xml`.
3. Click **Refresh Guide Data**.

Details: [Adding it to Jellyfin](#adding-it-to-jellyfin).

**Plex** (Live TV requires a paid **Plex Pass**). Go to Settings → Live TV & DVR → **Set Up Plex DVR**:
1. Enter the device address manually: `SERVER:8765`.
2. When Plex asks for a guide, choose XMLTV and enter `http://SERVER:8765/epg.xml`.

Details: [Adding it to Plex](#adding-it-to-plex).

*A fan project, not affiliated with spelunkr.tv, Everything Is Terrible! or RedLetterMedia.*

## How it works

Spelunkr doesn't host video streams. The website downloads a schedule (`lineup.json` plus one playlist per channel) and runs a scheduler in your browser. That scheduler decides what plays when: the shows, the text "bump" cards and music between them, the vintage commercial breaks, and the "Up Next" cards. The videos themselves play straight from the Internet Archive.

`spelunkr2jellyfin.py` includes a Python copy of that scheduler. It was checked against the site's own code: every channel matched to the millisecond at three different times (now, 10 days ahead, and 4 days back). For each viewer, the script turns the schedule into one continuous live stream:

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

The script re-checks the lineup every 30 minutes. Everything it downloads is saved in a `cache` folder (next to the script on Windows, in `/config` on Docker), so if Spelunkr's server is down when the bridge restarts, it starts from the last saved schedule.

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
- **GPU encoding:** with `/dev/dri` passed into the container, the bridge encodes on an Intel GPU through Quick Sync (or VAAPI) instead of the CPU. At startup it runs a 1-second test encode and logs `encoding on the GPU: Intel Quick Sync on /dev/dri/renderD128`. If the GPU doesn't work, it logs why and uses the CPU. Use `--gpu-device` to pick the same render node Jellyfin uses.
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
- `ads`: years, exclude and boost filters, boost weight. These pick from Spelunkr's own commercial reels.
  - `archive`: your own commercials from the [Internet Archive](https://archive.org), as a list of item ids, e.g. `["1993-mtv-commercials", {"id": "capture-a-644", "year": 1986}]` (the id is the part after `archive.org/details/`). Good fits are tapes of real commercial breaks, like the MTV and VH1 ones. Each tape is downloaded once in the background, split at its black frames into breaks of 1 to 3 minutes and volume-matched, then saved in the cache. Until a tape is done it's just left out, so a long list takes a little while to fill in the first time.
  - `"spelunkr": false`: use only your `archive` tapes, none of Spelunkr's.
- `mid_break_minutes` (default 8) and `ad_gap_minutes` (default 20).
- `card_seconds`: how long each bump card stays up. The examples use 20. The slides slow down evenly to fill the time, and a full station break (channel card plus network card) runs about 44 seconds. Leave it out for Spelunkr's own pacing.

Under `"youtube"`:
- `"playlist"` (one show) or `"shows"`: a list of `{"name", "playlists", "order"}`. `order` is `"oldest-first"` or the playlist's own order.
- `"breaks": "sponsorblock"` also puts commercial breaks mid-episode, where SponsorBlock users marked segment changes.
- `"skip"`: videos to leave out, as video ids or YouTube links, e.g. `["vcBkm1nOI5k", "https://www.youtube.com/watch?v=JkWk_chVcJk"]`. The example channels use it for videos YouTube age-restricts, so the guide is right from the first start. The bridge still finds new unplayable videos on its own and drops them. To see what it found, check `cache/custom__unplayable.json`; those videos are good candidates for your list.
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

### Unraid, step by step

**1. Get the template.** Open Unraid's terminal (the `>_` icon at the top right) and paste this whole line:
```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-spelunkr-bridge.xml https://raw.githubusercontent.com/bwhiteman12-code/spelunkr-bridge/main/unraid/spelunkr-bridge.xml
```
It should end with `my-spelunkr-bridge.xml’ saved`.

**2. Create the container.** Docker tab → **Add Container** → **Template** dropdown → **spelunkr-bridge** (under User templates). Then:
- **Public URL:** `http://YOUR-UNRAID-IP:8765`
- **GPU render node:** the one Jellyfin uses (Jellyfin → Dashboard → Playback → Transcoding → QSV Device), e.g. `/dev/dri/renderD128`. No Intel GPU? Remove the **Intel GPU** device and leave this empty; it uses the CPU.
- Leave the rest as is, then click **Apply**.

**3. Optional: add the example channels** (Hack Fraud TV and EISTV). In the terminal:
```bash
wget -O /mnt/user/appdata/spelunkr/channels.json https://raw.githubusercontent.com/bwhiteman12-code/spelunkr-bridge/main/examples/channels.json
```
Then restart the container (click its icon → **Restart**). To build your own channel instead, see [Make your own channel](#make-your-own-channel).

**4. Check the log** (container icon → **Logs**). When it's ready you'll see:
```
lineup loaded: … channels                  (all of Spelunkr's current channels, plus "(2 custom)" with the examples)
encoding on the GPU: Intel Quick Sync on /dev/dri/renderD128     (or "the CPU (x264)")
ready - M3U: http://YOUR-UNRAID-IP:8765/playlist.m3u
```
With the examples, the first start takes about 2 minutes while it reads the YouTube playlists. A few `can't be played (Sign in to confirm your age…)` warnings are normal; those videos are skipped.

**5. Add it to Jellyfin.** See [Adding it to Jellyfin](#adding-it-to-jellyfin).

**Updating:** whenever this repo changes, a new image is built within a few minutes. To install it, go to Docker tab → **Check for Updates**, then click **update ready** on the container. Your settings and `channels.json` are kept. To update automatically, install the **Auto Update Applications** plugin from the Apps tab. If the example channels change, re-run the command in step 3 to get the new copy (this replaces your `channels.json`).

### Other ways to set it up

**Unraid by hand** (without the template): Docker tab → Add Container with these settings:
- Repository: `ghcr.io/bwhiteman12-code/spelunkr-bridge:latest`, Network: Bridge
- Port: `8765` → `8765`
- Path: `/config` → `/mnt/user/appdata/spelunkr`
- Variable `PUBLIC_URL`: `http://YOUR-UNRAID-IP:8765`
- Optional: Device `/dev/dri` and Variable `GPU`: `/dev/dri/renderD128`

**Compose (Compose Manager plugin or any Docker host):** use [`docker-compose.yml`](docker-compose.yml). Set `PUBLIC_URL` and `TZ`, then run `docker compose up -d`.

### Other NAS systems

The image runs on any NAS or server with Docker. Use the [`docker-compose.yml`](docker-compose.yml) and change three things:
- the folder on the left of `:/config` to one on your NAS
- `PUBLIC_URL` to your NAS's IP address
- `TZ` to your timezone

| NAS / OS | Where to set it up |
|---|---|
| **Synology** (DSM 7.2+) | **Container Manager → Project → Create**. Create a folder first, e.g. `/volume1/docker/spelunkr`, and use it for `/config`. Paste the compose file. |
| **QNAP** | **Container Station → Applications → Create**. Paste the compose file, with a folder like `/share/Container/spelunkr`. |
| **TrueNAS SCALE** | **Apps → Discover Apps → Custom App**. Image `ghcr.io/bwhiteman12-code/spelunkr-bridge`, tag `latest`. Port 8765. A host path for `/config`. Environment variable `PUBLIC_URL`. |
| **OpenMediaVault** | **Services → Compose → Files → Create**. Paste the compose file. |
| **CasaOS, Portainer, plain Linux** | Import or paste the compose file, or `docker compose up -d`. |

Things to know:
- **GPU encoding is Intel only** in the Docker image (Quick Sync / VAAPI). Many Intel-based Synology and QNAP models have it. If your NAS has no `/dev/dri` folder (AMD, NVIDIA or ARM), **delete the `devices:` lines** from the compose file. Otherwise the container won't start. It then encodes on the CPU, which most x86 NASes handle fine at 640×480.
- **ARM NASes** (e.g. Synology "j" models) work with the 64-bit ARM image, but CPU encoding with the VHS card look is heavy for small chips. One stream is usually OK. To lighten it, add `--no-vhs-cards` to `EXTRA_ARGS`. 32-bit ARM isn't supported.
- **Your own channels:** put `channels.json` in the folder you mapped to `/config`, then restart the container.
- **Updating:** pull the image again and recreate the container. In most NAS apps that's an **Update** or **Pull** button on the project or container.
- Only Unraid and Windows have been tested so far. If you get it running on another NAS, an issue or pull request with your steps is welcome.

**Settings:**

| Variable | What it does |
|---|---|
| `PUBLIC_URL` | How Jellyfin reaches the container, e.g. `http://192.168.1.20:8765` |
| `GPU` | GPU render node, e.g. `/dev/dri/renderD128`. Empty tries each one. |
| `EXTRA_ARGS` | Any other [options](#options), e.g. `--skip vacation-slides --no-vhs-cards` |
| `UPDATE_YTDLP` | `true` (default) updates yt-dlp at every start |
| `TZ` | Your timezone, e.g. `America/Chicago` (Unraid sets this itself) |

The `/config` folder holds the saved schedules and your `channels.json`, if you add one (start from [`examples/`](examples/)). Music paths like `music/...` still find the built-in music; to use your own, put a `music` folder in `/config`.

**Updating with Compose:** `docker compose pull && docker compose up -d`.

## Setup on Windows (Jellyfin on your own PC)

1. Put this folder somewhere permanent, e.g. `C:\Spelunkr Bridge`. It must contain `spelunkr2jellyfin.py`, the two `star4000` fonts, the `music` folder and the `install-windows` files.
2. Double-click **`install-windows.bat`**. It will:
   - find Python 3, or offer to install it with winget
   - install or update yt-dlp, which the YouTube channels need
   - offer to install Node.js if you've added your own channels (YouTube needs it to unlock video streams)
   - find the ffmpeg that comes with Jellyfin (no separate install needed). With Plex instead, install ffmpeg first with `winget install Gyan.FFmpeg`.
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

## Setup on a seedbox (Linux without Docker)

**Only for Linux without Docker,** such as a shared seedbox (Whatbox, Ultra.cc, etc.) where Jellyfin or Plex runs and you have SSH but no root. On Windows, use the [Windows setup](#setup-on-windows-jellyfin-on-your-own-pc). Wherever Docker works (Unraid, a NAS, a Linux server you control), the [Docker image](#setup-on-unraid--docker) is simpler.

**1. Pick a port.** The bridge needs a port number of its own, which you choose:
- **On a shared seedbox,** use a port your provider assigned you. It's usually on the provider's control panel, under something like "ports". Other users share the machine, so don't just make one up.
- **On your own server,** any free number between 1024 and 65535 works, e.g. `8765`.

Below, `12345` is only an example. Replace it with your port.

**2. Install.** Over SSH, run this with your port at the end:
```bash
curl -fsSL https://raw.githubusercontent.com/bwhiteman12-code/spelunkr-bridge/main/install-seedbox.sh | sh -s 12345
```
It puts everything in your home folder:
- the bridge itself, in `~/spelunkr-bridge`
- ffmpeg, if it's missing
- yt-dlp and Node.js, for the YouTube channels
- the example channels

Then it starts the bridge, sets it to start again after a reboot, and waits until the channels are loaded. When it's done, it shows the two addresses for Jellyfin with your address and port filled in ([which address](#which-address-jellyfin-uses-seedbox)). The first run takes a few minutes. If something goes wrong, it says what.

**3. Add it to Jellyfin** as [below](#adding-it-to-jellyfin), using the two addresses the installer showed, e.g. `http://127.0.0.1:12345/playlist.m3u` and `http://127.0.0.1:12345/epg.xml`. If Jellyfin says *"There was an error saving the TV provider"*, see [Jellyfin can't add the tuner](#jellyfin-cant-add-the-tuner-seedbox).

- **Update it, or change the port:** run the same line again (with the new port, if it changed). After a port change, update the addresses in Jellyfin or Plex.
- **Stop it:** `pkill -f spelunkr2jellyfin.py`. Start it again with `nohup ~/spelunkr-start.sh </dev/null >/dev/null 2>&1 &`.
- **Log:** `~/spelunkr-bridge/spelunkr.log`
- **Usually no port forwarding needed.** The bridge normally listens only inside the seedbox ([details](#which-address-jellyfin-uses-seedbox)). Jellyfin relays the streams to your TVs and phones, so nobody connects to the bridge directly.
- **Light on the CPU:** seedboxes have no GPU and share their CPU, so it uses the fastest encoder setting and draws bump cards without the VHS look. Expect one or two people watching at once to be comfortable. To change the settings, edit the last line of `~/spelunkr-start.sh`, e.g. remove `--no-vhs-cards`. Running the install line again resets that file, so make the edit again after an update.
- **YouTube channels may stall.** YouTube often blocks seedbox (data-center) addresses, so the YouTube channels may show standby. The Spelunkr channels aren't affected.
- **Static ffmpeg is handled for you.** The ffmpeg the installer downloads can't look up web addresses (it crashes trying). The bridge notices this when it starts and downloads the videos for it instead. The log then says *"it reads videos through the local relay"*. Nothing to do.

### Which address Jellyfin uses (seedbox)

The installer checks this for you each time it runs:
- **Jellyfin shares the seedbox's network** (the usual case): the bridge listens on `127.0.0.1`, which only the seedbox itself can reach. Jellyfin uses `http://127.0.0.1:12345/...`.
- **Jellyfin runs in its own container** (SeedHost does this, for example): inside it, `127.0.0.1` is the container itself, not your account. The installer finds the address the container reaches the seedbox at (something like `172.17.0.1`) and listens only there. That address isn't reachable from the internet. Jellyfin uses `http://172.17.0.1:12345/...`.
- **If there's no such address,** the installer listens on every address and uses the seedbox's name (something like `dust.seedhost.eu`), and it warns you: **the streams can then be reached from the internet,** with no password, by anyone who knows the address and port.

It looks for Jellyfin (or Plex) running under your account. If it isn't running when you install, the installer assumes `127.0.0.1` and says so.

### Jellyfin can't add the tuner (seedbox)

If Jellyfin says *"There was an error saving the TV provider"* when you add the tuner:

1. **Check the bridge is running.** Over SSH:
   ```bash
   grep -- --host ~/spelunkr-start.sh
   ```
   That shows the address it listens on (after `--host`) and the one Jellyfin should use (after `--public-url`). Then try it, with the `--host` address in place of `ADDRESS` (`127.0.0.1` if it says `0.0.0.0`) and your port:
   ```bash
   curl -s http://ADDRESS:12345/playlist.m3u | head -n 3
   ```
   If that prints `#EXTM3U` and a channel, the bridge is fine and it's the address. If it prints nothing, see the log first.
2. **Make sure Jellyfin is running, then run the install line again.** It checks again which address Jellyfin can reach and shows the new addresses. Remove the old entries in Jellyfin, add the new ones, then run **Refresh Guide**.
3. **If it still fails,** your provider may block that port. Use one it assigned you.

Notes:
- **Use the addresses the installer shows.** `--public-url` is where the playlist points the streams. If Jellyfin can reach the playlist but not that address, the tuner adds but the channels won't play.
- **If the bridge says "already running"** when you start it, it is. Stop it first with `pkill -f spelunkr2jellyfin.py`.
- **Running the install line again rewrites `~/spelunkr-start.sh`,** so any edits you made to it are lost.

## Adding it to Jellyfin

Use your server's IP address below, or `localhost` if Jellyfin and the bridge are on the same Windows PC.

Dashboard → Live TV:
1. **Tuner Devices → + → M3U Tuner.** File or URL: `http://YOUR-SERVER-IP:8765/playlist.m3u`
2. **TV Guide Data Providers → + → XMLTV.** File or URL: `http://YOUR-SERVER-IP:8765/epg.xml`. The default categories already include movie, kids and sports. If you have other live TV, you can limit this guide to the Spelunkr tuner.
3. **Scheduled Tasks → Refresh Guide → ▶**

**Slow to start on one TV?** If a channel takes several seconds longer to start on one Android TV / Google TV box than on others, open the Jellyfin app's settings on that box and turn on **Direct stream Live TV**. The box then plays the bridge's stream as-is instead of waiting for Jellyfin to re-package it. The bridge's stream (H.264 and AAC at 640×480) plays fine on any of these boxes.

## Adding it to Plex

**Plex Live TV requires a paid Plex Pass subscription.** Without it, Plex doesn't offer Live TV & DVR at all, and this section won't work. Jellyfin is free.

Plex doesn't accept M3U tuners, only HDHomeRun network tuners. The bridge also acts like an HDHomeRun, so Plex can use it directly with no Threadfin or xTeVe in between. Use the same address and port as for Jellyfin: on Windows `127.0.0.1:8765`, on Docker your server's IP, on a seedbox the address the installer showed (usually `127.0.0.1`) and your port.

1. **Settings → Live TV & DVR → Set Up Plex DVR.** Plex looks for tuners. When it doesn't find this one on its own, choose to enter the device's network address, and type `YOUR-SERVER-IP:8765`.
2. **Continue** past the channel list. Every channel is listed, already numbered.
3. **When Plex asks for a guide,** choose the option to use an XMLTV guide, and enter `http://YOUR-SERVER-IP:8765/epg.xml`.
4. Plex matches each channel to the guide by its number. Check the list, then finish.

- **How many can watch:** the bridge tells Plex it has 4 tuners, so Plex lets 4 channels be watched at once. To allow more (or fewer), add e.g. `--tuners 6` to the [options](#options).
- **Windows:** Plex doesn't come with a usable ffmpeg, so install it once with `winget install Gyan.FFmpeg` before running `install-windows.bat`.
- **Channel changes** can take a few seconds longer than in Jellyfin, because Plex usually re-encodes live TV.
- **Jellyfin doesn't use any of this.** It works the same whether or not anyone uses Plex, and you can use both at once.

## Options

```
python spelunkr2jellyfin.py --help
  --port 8765               port to listen on
  --public-url URL          address Jellyfin uses to reach this server
  --size 640x480            output resolution (most tapes are 4:3 SD)
  --maxrate 2000            max video kbps
  --preset veryfast         x264 speed/quality (ultrafast = lower CPU)
  --hours 72                hours of guide to publish
  --tuners 4                for Plex: how many channels can be watched at once
  --lead 0                  run N seconds ahead of the website, to cancel out Jellyfin's buffering
  --hwaccel auto            GPU encoding: auto (Linux: Quick Sync, then VAAPI; Windows: NVIDIA, Quick Sync,
                            then AMD; then CPU), qsv, vaapi, nvenc, amf or none
  --gpu-device auto         Linux GPU render node, e.g. /dev/dri/renderD129 (auto: try each one)
  --host 0.0.0.0            address to listen on (127.0.0.1 = this PC only)
  --ffmpeg / --ffprobe      ffmpeg to use (default: on PATH, else Jellyfin's copy on Windows)
  --relay auto              download videos in Python and hand them to ffmpeg locally (auto: only
                            when ffmpeg can't look up web addresses itself, as with static builds
                            on seedboxes); on or off to force it
  --log-file FILE           also write the log to a file
  --no-static               skip the tune-in TV snow
  --no-logo-bug             skip the corner channel logo
  --no-vhs-cards            draw bump cards clean instead of as worn VHS
  --only suplex,primetime   only these channels
  --skip vacation-slides    leave channels out
  --font FILE               font for bump cards
  --cache DIR               where to save schedule files (default ./cache)
  --channels FILE           your own channels (default channels.json next to the script)
  --include-hidden          also show Spelunkr channels marked coming soon
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
