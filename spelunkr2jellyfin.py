#!/usr/bin/env python3
"""
spelunkr2jellyfin - turn spelunkr.tv's channels into live TV for Jellyfin.

spelunkr.tv has no video streams of its own: the website reads a published
schedule and plays Internet Archive MP4s in your browser at the right offset,
with text "bump" cards, music and vintage commercials between shows.
This server rebuilds that exact schedule (a port of the site's scheduling
engine, so it stays in sync with spelunkr.tv) and outputs a normal live
MPEG-TS stream per channel that Jellyfin (or VLC, Plex, Channels...) can tune.

Endpoints
  /playlist.m3u        M3U tuner list   -> Jellyfin "M3U Tuner"
  /epg.xml             XMLTV TV guide   -> Jellyfin "XMLTV" guide provider
  /stream/<id>.ts      live stream for one channel
  /logo/<id>.png       channel logo (rendered from the site's pixel art)
  /                    status page: what's on now

Needs Python 3.8+ (standard library only) and ffmpeg + ffprobe (on PATH, or
Jellyfin's own copy on Windows). YouTube channels also need yt-dlp.
"""
import argparse
import bisect
import copy
import functools
import html
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zlib
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape as xml_escape

BROADCAST = "https://jbuopkuluyfjwgsrigry.supabase.co/storage/v1/object/public/broadcast/"
AUDIO_BASE = "https://audio.spelunkr.tv/bumps/"
USER_AGENT = "spelunkr2jellyfin/2.0 (personal Jellyfin bridge)"
ENGINE_VERSIONS = {1, 2, 5}      # scheduler versions this port understands
REFRESH_SECONDS = 30 * 60        # re-check the lineup this often
KIDS_CHANNELS = {"saturday-morning", "toonertia", "latchkey-tv", "rubber-hose"}
SPORTS_CHANNELS = {"suplex"}
GOLD, PLATFORM_BG = "#ffd740", "#0a0e2a"
PLATFORM_BORDER = ["#2652b2", "#25469b", "#243a83", "#232c64"]

log = logging.getLogger("spelunkr")
CACHE_DIR = None
WINDOWS = os.name == "nt"
# On Windows, keep ffmpeg / yt-dlp from opening console windows when running in the background.
NO_WINDOW = {"creationflags": 0x08000000} if WINDOWS else {}


def _python():
    """python.exe for running yt-dlp (pythonw.exe has no console to read output from on some setups)."""
    exe = sys.executable
    if WINDOWS and os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(alt):
            return alt
    return exe


PYTHON = _python()


def find_tool(name):
    """ffmpeg/ffprobe: the FFMPEG/FFPROBE setting, then PATH, then Jellyfin's own copy on Windows."""
    env = os.environ.get(name.upper())
    if env:
        return env
    if shutil.which(name):
        return name
    if WINDOWS:
        roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "ProgramData")]
        for root in filter(None, roots):
            for sub in ("Jellyfin\\Server", "Programs\\Jellyfin\\Server", "Jellyfin\\Server\\ffmpeg"):
                p = os.path.join(root, sub, name + ".exe")
                if os.path.isfile(p):
                    return p
    return name


def now_ms():
    return int(time.time() * 1000)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_json(url, timeout=20):
    return json.loads(http_get(url, timeout).decode("utf-8"))


def fetch_broadcast(path, immutable=False):
    """Fetch a file from Spelunkr's broadcast folder, keeping a copy on disk.
    Versioned files never change, so those come from disk when we have them.
    If the network fails, the last good copy is used."""
    local = os.path.join(CACHE_DIR, path.replace("/", "__")) if CACHE_DIR else None
    if immutable and local and os.path.exists(local):
        with open(local, encoding="utf-8") as f:
            return json.load(f)
    try:
        raw = http_get(BROADCAST + path)
        data = json.loads(raw.decode("utf-8"))
        if local:
            try:
                tmp = local + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(raw)
                os.replace(tmp, local)
            except OSError as e:
                log.debug("couldn't save %s: %s", path, e)
        return data
    except Exception as e:
        if local and os.path.exists(local):
            log.warning("%s: network failed (%s), using saved copy", path, e)
            with open(local, encoding="utf-8") as f:
                return json.load(f)
        raise


_PERMANENT_YT = re.compile(r"confirm your age|age-restricted|inappropriate for some users|video unavailable|"
                           r"private video|has been removed|account .* terminated|copyright|not available|"
                           r"members-only|join this channel", re.I)


def is_permanent_yt_error(msg):
    """A YouTube error that won't go away by retrying (age gate, removed, private...), not a temporary block."""
    return bool(_PERMANENT_YT.search(msg or "")) and "bot" not in (msg or "").lower()


def archive_url(identifier, filename):
    return "https://archive.org/download/%s/%s" % (identifier, urllib.parse.quote(filename, safe="!'()*~"))


# ---------------------------------------------------------------------------
# JavaScript number semantics, so hashes and random picks match the site
# ---------------------------------------------------------------------------

def i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def u32(x):
    return x & 0xFFFFFFFF


def imul(a, b):
    return i32(a * b)


def utf16(s):
    b = s.encode("utf-16-le")
    return struct.unpack("<%dH" % (len(b) // 2), b)


def js_len(s):
    return len(s.encode("utf-16-le")) // 2


def js_hash(s):
    """The site's string hash (Java-style, 32-bit)."""
    t = 0
    for cu in utf16(s):
        t = i32((t << 5) - t + cu)
    return t


def js_rng(seed):
    """The site's seeded random generator (mulberry32)."""
    state = [i32(seed)]

    def nxt():
        state[0] = t = i32(state[0] + 1831565813)
        n = imul(i32(t ^ (u32(t) >> 15)), 1 | t)
        n = i32(n ^ (n + imul(i32(n ^ (u32(n) >> 7)), 61 | n)))
        return u32(n ^ (u32(n) >> 14)) / 4294967296
    return nxt


def js_year(v):
    """parseInt(year, 10) -> int or None."""
    if v is None:
        return None
    m = re.match(r"\s*([+-]?\d+)", str(v))
    return int(m.group(1)) if m else None


def js_lt(a, b):
    return utf16(a) < utf16(b)


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------------------
# The site's scheduling engine (bumps, commercial breaks, Up Next cards)
# ---------------------------------------------------------------------------

INTRO_MS, TAIL_MS, PAUSE_MS, GAP_MS, FADE_MS = 3500, 1000, 1000, 300, 2000
AD_GAP_MS = 20 * 60_000
SEASON_MS = 336 * 3600_000
PT = 1_000_000
VS = 20_000
DAY_MS = 24 * 3600_000
HOUR_MS = 3600_000
CARD_MS = 10_000
INF = float("inf")


def slide_ms(text):
    return max(2500, 1500 + js_len(text) * 55)


def slide_dur(s):
    """A slide's on-screen time: the site's rule, unless a custom channel stretched its cards ("_ms")."""
    return s["_ms"] if is_int(s.get("_ms")) else slide_ms(s["text"])


def slides_ms(slides):
    return sum(slide_dur(s) for s in slides)


def stretch_bumps(bumps, card_ms):
    """Custom channels: slow each card's slides down (keeping their relative pacing) so the whole
    card, with its fade-out and pause, lasts about card_ms. Cards already that long are left alone."""
    out = []
    for b in bumps:
        slides = [dict(s) for s in b.get("slides") or []]
        natural = sum(slide_ms(s["text"]) for s in slides)
        target = card_ms - TAIL_MS - PAUSE_MS
        if slides and natural < target:
            done = 0
            for i, s in enumerate(slides):
                s["_ms"] = (target - done if i == len(slides) - 1
                            else int(round(slide_ms(s["text"]) * target / natural)))
                done += s["_ms"]
        out.append(dict(b, slides=slides))
    return out


def full_break_ms(cb, pb):
    return INTRO_MS + slides_ms(cb["slides"]) + TAIL_MS + PAUSE_MS + GAP_MS + slides_ms(pb["slides"]) + TAIL_MS + PAUSE_MS


def _date_ms(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000


def in_season(rng_, t):
    a, b = rng_
    if re.match(r"^\d{4}-", a):
        return _date_ms(a + "T00:00:00Z") <= t <= _date_ms(b + "T23:59:59.999Z")

    def md(x):
        p = [int(v) for v in x.split("-")[:2]]
        return p[0] * 100 + p[1]
    d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
    o, c, l = d.month * 100 + d.day, md(a), md(b)
    return c <= o <= l if c <= l else (o >= c or o <= l)


_pools = {}


def bump_pools(bumps):
    key = id(bumps)
    p = _pools.get(key)
    if p is None or p[0] is not bumps:
        live = [b for b in bumps if b.get("active") is not False]
        p = (bumps, live, [b for b in live if not b.get("date_range")], [b for b in live if b.get("date_range")])
        _pools[key] = p
    return p[1:]


def eligible(bumps, t):
    live, year_round, seasonal = bump_pools(bumps)
    if any(in_season(b["date_range"], t) for b in seasonal):
        return [b for b in live if not b.get("date_range") or in_season(b["date_range"], t)]
    return year_round


def pick_bump(bumps, idx, chan, seed, t):
    if not bumps:
        return None
    pool = eligible(bumps, t)
    if not pool:
        return None
    r = js_rng(js_hash("%s-break-%s-%s" % (chan, idx, seed)))
    return pool[math.floor(r() * len(pool))]


def shuffle_deck(pool, recent, seed):
    r = js_rng(seed)
    s = list(pool)
    for l in range(len(s) - 1, 0, -1):
        u = math.floor(r() * (l + 1))
        s[l], s[u] = s[u], s[l]
    half = len(s) // 2
    hold = {}
    for l in range(min(half, len(recent)), 0, -1):
        hold[id(recent[-l])] = half + 1 - l
    if not hold:
        return s
    c = lambda b: hold.get(id(b), 0)
    for l in range(1, min(half, len(recent)) + 1):
        u = recent[-l]
        d = next((i for i, b in enumerate(s) if b is u), -1)
        if d < 0 or d >= c(u):
            continue
        h = [p for p in range(c(u), len(s)) if c(s[p]) <= d]
        if not h:
            continue
        f = h[math.floor(r() * len(h))]
        s[d], s[f] = s[f], s[d]
    return s


def deck_pick(state, bumps, chan, seed, t):
    if not bumps:
        return None
    pool = eligible(bumps, t)
    if not pool:
        return None
    _, _, seasonal = bump_pools(bumps)
    key = "|".join(b["id"] for b in seasonal if in_season(b["date_range"], t)) if seasonal else ""
    if state["key"] != key or state["pos"] >= len(state["order"]):
        state["order"] = shuffle_deck(pool, state["recent"], js_hash("%s-deck-%s-%s" % (chan, seed, state["rounds"])))
        state["rounds"] += 1
        state["pos"] = 0
        state["key"] = key
    b = state["order"][state["pos"]]
    state["pos"] += 1
    state["recent"].append(b)
    if len(state["recent"]) > len(state["order"]):
        state["recent"].pop(0)
    return b


def pick_audio(tracks, bump_id, idx, seed):
    if not tracks:
        return None
    r = js_rng(js_hash("%s-audio-%s-%s" % (bump_id, idx, seed)))
    o = math.floor(r() * len(tracks))
    start = math.floor(r() * 60)
    fallbacks = [tracks[(o + d) % len(tracks)] for d in range(1, len(tracks))][:3]
    return dict(tracks[o], startOffset=start, fallbacks=fallbacks)


def pick_sting(stings, idx, chan, seed):
    if not stings:
        return None
    r = js_rng(js_hash("%s-card-%s-%s" % (chan, idx, seed)))
    return stings[math.floor(r() * len(stings))]


def ad_phases(slices):
    keys = [math.floor(js_rng(js_hash("ad-%s" % s["id"]))() * PT) for s in slices]

    def cmp(a, b):
        if keys[a] != keys[b]:
            return keys[a] - keys[b]
        ia, ib = slices[a]["id"], slices[b]["id"]
        return -1 if js_lt(ia, ib) else 1 if js_lt(ib, ia) else a - b
    out = [0] * len(slices)
    for pos, i in enumerate(sorted(range(len(slices)), key=functools.cmp_to_key(cmp))):
        out[i] = math.floor(pos * PT / len(slices))
    return out


def ad_rates(items, block, gap, slice_ms):
    s = i = o = c = 0
    by_year = OrderedDict()
    n = len(items)
    for d, u in enumerate(items):
        h = u["duration"] * 1000
        s += h
        i += h
        o += h
        if block and d < n - 1 and o < block["lengthMs"]:
            s += block["cardMs"]
            i += block["cardMs"]
            o += block["cardMs"]
            continue
        o = 0
        f = items[(d + 1) % n]
        if u.get("offAir") is not True and f.get("offAir") is not True and i >= gap:
            c += 1
            y = js_year(u.get("year"))
            by_year[y] = by_year.get(y, 0) + 1
            s += VS + slice_ms
            i = VS / 2
        else:
            s += VS
            i += VS
    return s, by_year


class AdPlan:
    """Which commercial plays at each break: era-matched, spread out, no repeats within a day."""

    def __init__(self, slices, lineup, block, gap, cid, start, decades):
        self.slices = slices
        self.phases = ad_phases(slices)
        avg = sum(s["lengthMs"] for s in slices) / len(slices)
        self.cycle_ms, self.by_year = ad_rates(lineup, block, gap, avg)
        self.years = [js_year(s.get("year")) for s in slices]
        self.decades = "d%d" % decades if is_int(decades) and decades >= 1 else None
        st = start if is_int(start) and 0 <= start < 1000 else u32(js_hash("ads-%s" % cid)) % 1000
        self.start = st * (PT // 1000)
        self._tiers = {}
        self._cache = {}

    def _tier(self, g, v):
        key = (g, v)
        if key in self._cache:
            return self._cache[key]
        w = int(v[1:]) if isinstance(v, str) else None

        def ok(L):
            if v == INF:
                return True
            if L is None or g is None:
                return False
            if w is not None:
                return abs(math.floor(L / 10) - math.floor(g / 10)) <= w
            return abs(L - g) <= v
        ph, sl = self.phases, self.slices

        def cmp(a, b):
            if ph[a] != ph[b]:
                return ph[a] - ph[b]
            ia, ib = sl[a]["id"], sl[b]["id"]
            return -1 if js_lt(ia, ib) else 1 if js_lt(ib, ia) else a - b
        lst = sorted([i for i in range(len(sl)) if sl[i].get("anywhere") is True or ok(self.years[i])],
                     key=functools.cmp_to_key(cmp))
        e = sum(m for y, m in self.by_year.items() if ok(y))
        need = e * SEASON_MS / self.cycle_ms if self.cycle_ms > 0 else 0
        healthy = len(lst) > 0 and len(lst) >= need
        round_ms = SEASON_MS if (healthy or not lst) else max(HOUR_MS, math.floor(len(lst) * self.cycle_ms / e))
        t = {"tol": v, "list": lst, "places": [ph[i] for i in lst], "roundMs": round_ms, "healthy": healthy}
        self._cache[key] = t
        return t

    def tiers(self, g):
        if g in self._tiers:
            return self._tiers[g]
        if g is None:
            out = [self._tier(None, INF)]
        else:
            v = [self._tier(g, w) for w in [3, 6] + ([self.decades] if self.decades else []) + [INF]]
            first = next(i for i, t in enumerate(v) if t["healthy"] or t["tol"] == INF)
            out = v[first:]
        self._tiers[g] = out
        return out

    @staticmethod
    def _phase(e, t, n):
        r = (e % t + t) % t
        return (math.floor(r * PT / t) + n) % PT

    def pick(self, used, year, now, since):
        if len(used) >= len(self.phases):
            used.clear()
        tiers = [t for t in self.tiers(year) if t["list"]]
        marks = []
        for t in tiers:
            u, places, rm = len(t["list"]), t["places"], t["roundMs"]
            d = self._phase(since, rm, self.start)
            h = self._phase(now, rm, self.start)
            f = 0 if now <= since else (PT if now - since >= rm else (h - d + PT) % PT)
            p = math.floor(min(DAY_MS, rm) * PT / rm)
            m = bisect.bisect_left(places, d + 1)
            marks.append(m)
            for g in range(u):
                v = (m + g) % u
                if (((places[v] - d + PT) % PT) or PT) > f:
                    break
                if t["list"][v] not in used:
                    used.add(t["list"][v])
                    return t["list"][v]
            for g in range(1, u + 1):
                v = (m - g + u * 2) % u
                if (d - places[v] + PT) % PT >= p:
                    break
                if t["list"][v] not in used:
                    used.add(t["list"][v])
                    return t["list"][v]
        for k, t in enumerate(tiers):
            u = len(t["list"])
            for h in range(1, u + 1):
                f = t["list"][(marks[k] - h + u * 2) % u]
                if f not in used:
                    used.add(f)
                    return f
        return -1


class Version:
    """One published version of a channel: a looping playlist anchored at epochMs,
    with bumps, commercial breaks and Up Next cards placed exactly like the site."""

    def __init__(self, cid, meta, manifest, details, platform):
        self.cid = cid
        self.v = meta["v"]
        self.epoch = int(meta["epochMs"])
        self.effective = int(meta.get("effectiveAtMs") or self.epoch)
        self.items = manifest["items"]
        if not self.items or any(not is_int(it.get("duration")) or it["duration"] <= 0 for it in self.items):
            raise ValueError("items need whole-second durations")
        si = meta.get("startIndex") or 0
        self.start_index = si if 0 <= si < len(self.items) else 0
        self.engine = manifest.get("engine") or 1
        if self.engine not in ENGINE_VERSIONS:
            log.warning("%s v%s uses scheduler engine %s, which this script doesn't know yet - "
                        "timing may drift from spelunkr.tv", cid, self.v, self.engine)
        self.block = manifest.get("block") or None
        ads = manifest.get("ads") or None
        self.ads = ads if ads and ads.get("slices") else None
        self.mid_gap = (ads or {}).get("midBreakMs") if is_int((ads or {}).get("midBreakMs")) else 8 * 60_000
        cb, pb = manifest.get("bumps") or [], (platform or {}).get("bumps") or []
        if cb and pb:
            pools = manifest.get("audioPools") or {}
            self.bumps = {
                "channel": cb, "platform": pb,
                "channelAudio": [t for v in pools.values() for t in v] if manifest.get("audioOverlay") else (manifest.get("audio") or []),
                "platformAudio": (platform or {}).get("audio") or [],
            }
        else:
            self.bumps = None
        # custom channels: bump cards tagged with "show" only follow that show (untagged ones go anywhere)
        self.show_pools = {}
        if self.bumps and any(x.get("show") for x in cb):
            for name in {it.get("show") or "" for it in self.items} | {""}:
                pool = [x for x in cb if not x.get("show") or x["show"] == name]
                self.show_pools[name] = pool or cb
        self.details = (details or {}).get("items") or []
        self.fwd, self.back = [], []
        self.ad_carry, self.deck_carry = {}, {}
        self.built = OrderedDict()
        self.lock = threading.RLock()
        self._plan = None

    # -- building one loop through the playlist --------------------------------

    def _ad_plan(self):
        if self._plan is None:
            gap = self.ads.get("gapMs") if is_int(self.ads.get("gapMs")) else AD_GAP_MS
            self._plan = AdPlan(self.ads["slices"], self.items, self.block, gap, self.cid,
                                self.ads.get("start"), self.ads.get("decades"))
        return self._plan

    def _cycle(self, k, cycle_start=None, season_at=None):
        first = self.start_index if k == 0 else 0
        items = self.items[first:]
        if not self.bumps:
            return [{"type": "video", "dur": it["duration"] * 1000, "videoIndex": first + i} for i, it in enumerate(items)]
        b, seed, cid, block = self.bumps, "%s.%s" % (self.v, k), self.cid, self.block
        slices = self.ads["slices"] if self.ads else None
        carry = self.ad_carry.get(k) if (slices and k >= 1) else None
        ad_out = {} if (slices and k >= 0) else None
        deck = self.engine >= 5
        dc = self.deck_carry.get(k) if (deck and k >= 1) else None

        def fresh(d):
            return ({"order": d["order"], "pos": d["pos"], "key": d["key"], "recent": list(d["recent"]), "rounds": 0}
                    if d else {"order": [], "pos": 0, "key": None, "recent": [], "rounds": 0})
        tagged = bool(self.show_pools)
        state = (None if tagged else fresh(dc)) if deck else None
        states = {name: fresh(d) for name, d in (dc or {}).items()} if (deck and tagged) else {}
        used = set(i for i, _ in (carry or {}).get("recent", []) if is_int(i) and 0 <= i < len(slices or []))
        since_carry = carry.get("fromMs") if carry and is_int(carry.get("fromMs")) else None
        placed = []
        gap = (self.ads.get("gapMs") if is_int(self.ads.get("gapMs")) else AD_GAP_MS) if self.ads else AD_GAP_MS
        plan = self._ad_plan() if slices else None
        h = cycle_start or 0
        f = I = card_no = 0
        out = []
        for F, it in enumerate(items):
            U = first + F
            dur = it["duration"] * 1000
            out.append({"type": "video", "dur": dur, "videoIndex": U})
            h += dur
            f += dur
            I += dur
            if block and F < len(items) - 1 and f < block["lengthMs"]:
                sting = pick_sting(block.get("stings"), U, cid, seed)
                z = sting["lengthMs"] if sting else block["cardMs"]
                out.append({"type": "up-next", "dur": z, "breakIndex": U, "cardIndex": card_no, "sting": sting})
                card_no += 1
                h += z
                f += z
                I += z
                continue
            f = 0
            # custom channels only: a "cont" part continues the same episode, so the break after it is
            # a commercial break if one is due (midBreakMs since the last one), otherwise no break at all
            cont = it.get("cont") is True
            if cont and not (slices and I >= self.mid_gap):
                continue
            x = season_at if season_at is not None else h
            if tagged:                  # one deck per show, so cards match what just aired
                name = it.get("show") or ""
                pool = self.show_pools.get(name) or self.show_pools[""]
                q = (deck_pick(states.setdefault(name, fresh(None)), pool, "%s/%s" % (cid, name), seed, x) if deck
                     else pick_bump(pool, U, cid, seed, x))
            else:
                q = deck_pick(state, b["channel"], cid, seed, x) if deck else pick_bump(b["channel"], U, cid, seed, x)
            se = pick_bump(b["platform"], U, "spelunkr", seed, x)
            if not (q and se):
                continue
            ca = pick_audio(b["channelAudio"], q["id"], U, seed)
            pa = pick_audio(b["platformAudio"], se["id"], U, seed)
            nxt = items[F + 1] if F < len(items) - 1 else self.items[0]
            if slices and it.get("offAir") is not True and nxt.get("offAir") is not True and (cont or I >= gap):
                idx = plan.pick(used, js_year(it.get("year")), h, since_carry if since_carry is not None else h - I)
                since_carry = None
                placed.append([idx, h])
                if idx < 0:
                    idx = 0
                sl = slices[idx]
                z = INTRO_MS + slides_ms(q["slides"]) + TAIL_MS + PAUSE_MS
                vv = slides_ms(se["slides"]) + TAIL_MS + PAUSE_MS
                out.append({"type": "bump-break", "half": "open", "dur": z, "breakIndex": U,
                            "channelBump": q, "channelAudio": ca})
                out.append({"type": "ad", "dur": sl["lengthMs"], "breakIndex": U, "slice": sl, "sliceIndex": idx})
                out.append({"type": "bump-break", "half": "close", "dur": vv, "breakIndex": U,
                            "platformBump": se, "platformAudio": pa})
                h += z + sl["lengthMs"] + vv
                I = vv
                if ad_out is not None:
                    ad_out["fromMs"] = h
                continue
            be = full_break_ms(q, se)
            out.append({"type": "bump-break", "half": None, "dur": be, "breakIndex": U, "channelBump": q,
                        "platformBump": se, "channelAudio": ca, "platformAudio": pa})
            h += be
            I += be
        if slices and ad_out is not None:
            if not placed:
                ad_out["fromMs"] = since_carry if since_carry is not None else (cycle_start if cycle_start is not None else 0)
            cut = h - DAY_MS
            ad_out["recent"] = [r for r in ((carry or {}).get("recent", []) + placed) if r[1] >= cut]
        if slices and k >= 0 and (k + 1) not in self.ad_carry:
            self.ad_carry[k + 1] = ad_out
        if deck and k >= 0 and (k + 1) not in self.deck_carry:
            keep = lambda st: {"order": st["order"], "pos": st["pos"], "key": st["key"], "recent": st["recent"]}
            self.deck_carry[k + 1] = {n: keep(st) for n, st in states.items()} if tagged else keep(state)
        return out

    def _span(self, k):
        """(start ms, length ms) of loop k; loops are built in order because each carries state into the next."""
        if k >= 0:
            while len(self.fwd) <= k:
                n = len(self.fwd)
                start = self.fwd[-1][0] + self.fwd[-1][1] if self.fwd else self.epoch
                segs = self._cycle(n, cycle_start=start)
                self.fwd.append((start, sum(s["dur"] for s in segs)))
                self._remember(n, segs)
            return self.fwd[k]
        r = -k - 1
        while len(self.back) <= r:
            n = len(self.back)
            end = self.back[-1][0] if self.back else self.epoch
            segs = self._cycle(-(n + 1), season_at=self.epoch)
            length = sum(s["dur"] for s in segs)
            self.back.append((end - length, length))
        return self.back[r]

    def _remember(self, k, segs):
        off = 0
        for s in segs:
            s["off"] = off
            off += s["dur"]
        self.built[k] = (segs, [s["off"] for s in segs])
        self.built.move_to_end(k)
        while len(self.built) > 8:
            self.built.popitem(last=False)

    def _entries(self, k):
        hit = self.built.get(k)
        if hit:
            self.built.move_to_end(k)
            return hit
        start = self._span(k)[0]
        segs = self._cycle(k, cycle_start=start) if k >= 0 else self._cycle(k, season_at=self.epoch)
        self._remember(k, segs)
        return self.built[k]

    def _loop_at(self, t):
        if t >= self.epoch:
            k = 0
            if self.fwd:
                lo, hi = 0, len(self.fwd) - 1
                last = self.fwd[-1]
                if t < last[0] + last[1]:
                    while lo < hi:
                        mid = (lo + hi + 1) // 2
                        if self.fwd[mid][0] <= t:
                            lo = mid
                        else:
                            hi = mid - 1
                    return lo
                k = len(self.fwd) - 1
            while True:
                s, l = self._span(k)
                if t < s + l:
                    return k
                k += 1
        k = -1
        while self._span(k)[0] > t:
            k -= 1
        return k

    def locate(self, t):
        """(entry, absolute start ms) on air at time t."""
        with self.lock:
            k = self._loop_at(t)
            start = self._span(k)[0]
            segs, offs = self._entries(k)
            i = max(0, bisect.bisect_right(offs, t - start) - 1)
            return segs[i], start + segs[i]["off"]

    def detail(self, idx):
        d = self.details[idx] if idx < len(self.details) else None
        return d if isinstance(d, dict) else {}


class Catalog:
    def __init__(self, include_hidden=False, only=None, skip=None, custom_file=None):
        self.include_hidden = include_hidden
        self.only, self.skip = set(only or []), set(skip or [])
        self.custom_file = custom_file
        self.lock = threading.Lock()
        self.channels = []
        self.versions = {}
        self._vcache = {}
        self._platforms = {}
        self._urls = {}
        self._playlists = {}      # custom channel id -> (fetched at, items)
        self._lineup = None
        self._descriptions = None # YouTube video id -> short description
        self._desc_busy = False
        self.fetch_descriptions = True
        self._unplayable = None   # YouTube video id -> why it can't be played (age-restricted, removed...)
        self._refresh_lock = threading.Lock()
        self._refresh_soon = False
        self._desc_tried = set()

    def mark_unplayable_done(self):
        """After marking videos unplayable in bulk: rebuild the lineup once, in the background."""
        self.mark_unplayable("", "", refresh=True)

    def _load_unplayable(self):
        if self._unplayable is None:
            self._unplayable = {}
            path = os.path.join(CACHE_DIR, "custom__unplayable.json") if CACHE_DIR else None
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._unplayable = json.load(f)
        return self._unplayable

    def mark_unplayable(self, vid, reason, refresh=True):
        """Drop a YouTube video that can't be played from the lineup (the change takes over quickly)."""
        bad = self._load_unplayable()
        if vid:
            if vid in bad:
                return
            bad[vid] = reason[:200]
            log.warning("YouTube video %s can't be played (%s) - taking it out of the lineup", vid, reason[:80])
            if CACHE_DIR:
                with open(os.path.join(CACHE_DIR, "custom__unplayable.json"), "w", encoding="utf-8") as f:
                    json.dump(bad, f)
        if refresh and not self._refresh_soon:
            self._refresh_soon = True

            def later():
                time.sleep(2)
                self._refresh_soon = False
                try:
                    self.refresh()
                except Exception as e:
                    log.warning("lineup refresh failed: %s", e)
            threading.Thread(target=later, daemon=True).start()

    def refresh(self):
        with self._refresh_lock:
            self._refresh()

    def _refresh(self):
        lineup = fetch_broadcast("lineup.json")
        self._lineup = lineup
        global AUDIO_BASE
        if isinstance(lineup.get("audioBase"), str) and lineup["audioBase"]:
            AUDIO_BASE = lineup["audioBase"]
        chans, vers = [], {}
        for c in lineup.get("channels", []):
            cid = c["id"]
            if self.only and cid not in self.only or cid in self.skip:
                continue
            if not self.include_hidden and (c.get("comingSoon") or c.get("debugOnly") or c.get("dial") is None):
                continue
            vl = []
            for m in c.get("versions") or []:
                key = (cid, m["v"])
                ver = self._vcache.get(key)
                if ver is None:
                    try:
                        manifest = fetch_broadcast(m["manifest"], immutable=True)
                        platform = None
                        if manifest.get("platform"):
                            if manifest["platform"] not in self._platforms:
                                self._platforms[manifest["platform"]] = fetch_broadcast(manifest["platform"], immutable=True)
                            platform = self._platforms[manifest["platform"]]
                        details = None
                        if m.get("details"):
                            try:
                                details = fetch_broadcast(m["details"], immutable=True)
                            except Exception:
                                pass
                        ver = Version(cid, m, manifest, details, platform)
                    except Exception as e:
                        log.warning("%s v%s: skipped (%s)", cid, m.get("v"), e)
                        continue
                    self._vcache[key] = ver
                vl.append(ver)
            if vl:
                vl.sort(key=lambda x: x.v)
                chans.append(c)
                vers[cid] = vl
        n_spelunkr = len(chans)
        self._add_custom(chans, vers)
        chans.sort(key=lambda c: (c.get("dial") is None, c.get("dial") or 0))
        with self.lock:
            self.channels, self.versions = chans, vers
        extra = len(chans) - n_spelunkr
        log.info("lineup loaded: %d channels%s", len(chans), " (%d custom)" % extra if extra else "")

    # -- custom channels from channels.json --------------------------------------

    def _ad_pool(self, rule):
        """Commercials for a custom channel, picked from Spelunkr's own ad reels
        (read from every Spelunkr playlist, even channels left out with --only/--skip)."""
        seen = {}
        for c in (self._lineup or {}).get("channels", []):
            for m in c.get("versions") or []:
                try:
                    manifest = fetch_broadcast(m["manifest"], immutable=True)
                except Exception:
                    continue
                for s in ((manifest.get("ads") or {}).get("slices") or []):
                    seen.setdefault(s["id"], s)
        lo, hi = (rule.get("years") or [0, 9999])[:2]
        skip = re.compile(rule["exclude"], re.I) if rule.get("exclude") else None
        boost = re.compile(rule["boost"], re.I) if rule.get("boost") else None
        weight = max(1, int(rule.get("boost_weight", 1)))
        pool = []
        for sid in sorted(seen):
            s = seen[sid]
            y, text = js_year(s.get("year")), "%s %s" % (s.get("tape", ""), s.get("file", ""))
            if y is None or not lo <= y <= hi or (skip and skip.search(text)):
                continue
            pool.append(s)
            if boost and boost.search(text):     # extra copies make these come up more often
                pool += [dict(s, id="%s#%d" % (sid, k)) for k in range(2, weight + 1)]
        return pool

    def _playlist(self, cid, url):
        """One YouTube playlist's videos in playlist order, re-read every 6 hours
        (falls back to the saved copy if YouTube can't be reached)."""
        hit = self._playlists.get(url)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        tag = re.sub(r"[^A-Za-z0-9_-]", "", url.split("list=")[-1])[:60] or "playlist"
        saved = os.path.join(CACHE_DIR, "custom__%s__%s.json" % (cid, tag)) if CACHE_DIR else None
        try:
            out = subprocess.run([PYTHON, "-m", "yt_dlp", "--flat-playlist", "-J", "--no-warnings", url],
                                 capture_output=True, text=True, encoding="utf-8", timeout=300, **NO_WINDOW)
            if out.returncode != 0:
                raise RuntimeError((out.stderr or "").strip().splitlines()[-1:] or "yt-dlp failed")
            items = []
            for en in json.loads(out.stdout).get("entries") or []:
                d, title = en.get("duration"), en.get("title") or ""
                if not en.get("id") or not d or title in ("[Private video]", "[Deleted video]"):
                    continue
                it = {"id": en["id"], "duration": int(d), "title": title, "youtube": True}
                if en.get("timestamp"):
                    it["year"] = str(datetime.fromtimestamp(en["timestamp"], tz=timezone.utc).year)
                items.append(it)
            if not items:
                raise RuntimeError("playlist is empty")
            if saved:
                with open(saved, "w", encoding="utf-8") as f:
                    json.dump(items, f)
        except Exception as e:
            if hit:
                log.warning("%s: couldn't re-read %s (%s); keeping the current copy", cid, url, e)
                items = hit[1]
            elif saved and os.path.exists(saved):
                log.warning("%s: couldn't read %s (%s); using the saved copy", cid, url, e)
                with open(saved, encoding="utf-8") as f:
                    items = json.load(f)
            else:
                raise
        self._playlists[url] = (time.time(), items)
        return items

    def _try_playlist(self, cid, url):
        try:
            self._playlist(cid, url)
        except Exception:
            pass                                 # reported when the lineup is put together

    _PART = re.compile(r"[\s(\[\-–:,]*\bpart\s*(\d+)(\s*(of|/)\s*\d+)?[)\]]?", re.I)

    @classmethod
    def _programs(cls, items):
        """Group consecutive "Part 1 of 3" style uploads into one program."""
        progs = []
        for it in items:
            m = cls._PART.search(it["title"])
            base = re.sub(r"\s+", " ", cls._PART.sub("", it["title"])).strip(" -–:") if m else None
            key = base.lower() if base else None
            if key and progs and progs[-1]["key"] == key:
                progs[-1]["items"].append(it)
            else:
                progs.append({"key": key, "title": base or it["title"], "items": [it]})
        return progs

    def _youtube_items(self, ch):
        """The channel's videos in airing order. Each show keeps its own order; with several shows they're
        spread evenly through the rotation like a TV schedule, and multi-part uploads stay together."""
        cid, yt = ch["id"], ch["youtube"]
        shows = yt.get("shows") or [{"name": ch.get("name"), "playlists": [yt["playlist"]], "order": yt.get("order")}]
        # videos to leave out: ones found unplayable, plus the channel's own "skip" list
        # (video ids or YouTube links), e.g. known age-restricted uploads or ones you just don't want
        bad = set(self._load_unplayable())
        for s in yt.get("skip") or []:
            m = re.search(r"(?:v=|youtu\.be/|shorts/|^)([A-Za-z0-9_-]{11})(?:$|[?&#/])", str(s).strip())
            if m:
                bad.add(m.group(1))
        urls = [pl if isinstance(pl, str) else pl["url"] for show in shows for pl in show["playlists"]]
        if len(urls) > 3:                        # big lineups: read the playlists 6 at a time
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(6) as ex:
                list(ex.map(lambda u: self._try_playlist(cid, u), urls))
        videos = []
        for show in shows:
            vids = []
            for pl in show["playlists"]:
                url, order = (pl, show.get("order")) if isinstance(pl, str) else (pl["url"], pl.get("order", show.get("order")))
                try:
                    got = [it for it in self._playlist(cid, url) if it["id"] not in bad]
                    if yt.get("title_strip"):    # e.g. a channel name every upload repeats
                        got = [dict(it, title=re.sub(yt["title_strip"], "", it["title"]).strip() or it["title"])
                               for it in got]
                except Exception as e:
                    if len(shows) == 1:
                        raise
                    log.warning("%s: skipping %s (%s)", cid, url, e)
                    continue
                vids.append((order, got))
            videos.append(vids)
        if yt.get("unique"):
            # a video on several playlists airs once, under its most specific show: the shortest one
            # that still runs at least min_show_minutes (so one-clip playlists don't become 1-minute blocks)
            length = [sum(it["duration"] for _, g in vids for it in g) for vids in videos]
            floor = float(yt.get("min_show_minutes") or 10) * 60
            home = {}
            for s, vids in enumerate(videos):
                for _, g in vids:
                    for it in g:
                        home.setdefault(it["id"], []).append(s)
            home = {v: min(ss, key=lambda s: (length[s] < floor, length[s], s)) for v, ss in home.items()}
            videos = [[(o, [it for it in g if home[it["id"]] == s and not seen.add(it["id"])])
                       for o, g in vids] for s, vids, seen in ((s, vids, set()) for s, vids in enumerate(videos))]
        block_s = float(yt.get("block_minutes") or 0) * 60
        lineups, names = [], []
        for show, vids in zip(shows, videos):
            progs = []
            for order, got in vids:
                p = self._programs(got)
                if order == "oldest-first":
                    p.reverse()
                progs += p
            if block_s and progs:
                progs = self._blocks(progs, block_s, show.get("name") or "")
            for p in progs:
                n = len(p["items"])
                for i, it in enumerate(p["items"]):
                    it["show"] = show.get("name") or ""
                    it["prog"] = p["items"][0]["id"]
                    if n > 1:
                        it["progTitle"] = p["title"]
                        if i < n - 1:
                            it["cont"] = True       # the next part follows straight on
                    if p.get("desc"):
                        it["progDesc"] = p["desc"]
            if progs:
                lineups.append(progs)
                names.append(show.get("name") or "")
        # even spread: program k of a show with n programs sits at (k + 0.5) / n through the rotation.
        # With "stagger", each show gets its own offset instead of 0.5, so one-block shows don't all bunch up.
        def phase(s):
            if not yt.get("stagger"):
                return 0.5
            return int(__import__("hashlib").md5(names[s].encode("utf-8")).hexdigest()[:8], 16) / 2 ** 32
        slots = sorted(((k + phase(s)) / len(progs), s, k) for s, progs in enumerate(lineups) for k in range(len(progs)))
        return [dict(it) for _, s, k in slots for it in lineups[s][k]["items"]]

    @staticmethod
    def _blocks(progs, block_s, name):
        """Themed blocks: run a show's short clips back to back in programs of about block_s seconds
        (one guide listing each, no bump cards inside; commercials still come every 20+ minutes)."""
        blocks, cur, t = [], [], 0
        for p in progs:
            cur.append(p)
            t += sum(it["duration"] for it in p["items"])
            if t >= block_s * 0.85:
                blocks.append(cur)
                cur, t = [], 0
        if cur:
            if blocks and t < block_s * 0.4:
                blocks[-1] += cur                # a short leftover joins the block before it
            else:
                blocks.append(cur)
        out = []
        for b in blocks:
            items = [it for p in b for it in p["items"]]
            titles = [p["title"] for p in b]
            desc = ("On the tape: " + " / ".join(titles))[:900] if len(titles) > 1 else None
            out.append({"key": None, "title": name, "items": items, "desc": desc})
        return out

    # -- YouTube episode descriptions (for the guide) ---------------------------------

    @staticmethod
    def _clean_description(text):
        """Keep the part of a YouTube description that describes the episode, not the links and plugs."""
        keep = []
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                if keep:
                    break                # first paragraph only
                continue
            if re.search(r"https?://|www\.|patreon|merch|subscribe|#\w|instagram|twitter|facebook|discord", s, re.I):
                continue
            keep.append(s)
        out = " ".join(keep)
        return (out[:597] + "...") if len(out) > 600 else out

    def description(self, vid):
        return (self._descriptions or {}).get(vid) or None

    def _load_descriptions(self):
        if self._descriptions is None:
            self._descriptions = {}
            path = os.path.join(CACHE_DIR, "custom__descriptions.json") if CACHE_DIR else None
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._descriptions = json.load(f)

    def _fetch_descriptions(self, ids):
        """Fill in missing descriptions in the background, 25 videos per yt-dlp call, saving as it goes."""
        path = os.path.join(CACHE_DIR, "custom__descriptions.json") if CACHE_DIR else None
        bad = self._load_unplayable()
        todo = [v for v in dict.fromkeys(ids) if v not in self._descriptions and v not in self._desc_tried and v not in bad]
        if not todo or self._desc_busy:
            return
        self._desc_busy = True

        def work():
            got = 0
            try:
                for i in range(0, len(todo), 25):
                    batch = todo[i:i + 25]
                    out = subprocess.run([PYTHON, "-m", "yt_dlp", "--no-warnings", "--skip-download",
                                          "--ignore-errors", "--print", "%(id)s\t%(description)j"]
                                         + ["https://www.youtube.com/watch?v=" + v for v in batch],
                                         capture_output=True, text=True, encoding="utf-8", timeout=900, **NO_WINDOW)
                    for line in out.stdout.splitlines():
                        vid, _, desc = line.partition("\t")
                        try:
                            self._descriptions[vid] = self._clean_description(json.loads(desc) or "")
                            got += 1
                        except ValueError:
                            pass
                    dropped = False                  # this pass also finds videos that can't be played at all
                    for line in (out.stderr or "").splitlines():
                        m = re.search(r"ERROR: \[youtube\] ([\w-]{11}): (.+)", line)
                        if m and is_permanent_yt_error(m.group(2)):
                            self.mark_unplayable(m.group(1), m.group(2), refresh=False)
                            dropped = True
                    self._desc_tried.update(batch)   # don't retry failures until the next start
                    if dropped:
                        self.mark_unplayable_done()
                    if path:
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(self._descriptions, f)
            except Exception as e:
                log.warning("episode descriptions: %s", e)
            finally:
                self._desc_busy = False
                log.info("episode descriptions: fetched %d of %d", got, len(todo))
        log.info("episode descriptions: fetching %d in the background", len(todo))
        threading.Thread(target=work, daemon=True).start()

    def _break_marks(self, cid, items):
        """Where each episode moves on to its next segment, from SponsorBlock's crowd-sourced
        'intro' markers (the show's own title stings). Saved to disk; re-checked weekly."""
        path = os.path.join(CACHE_DIR, "custom__%s__sponsorblock.json" % cid) if CACHE_DIR else None
        marks = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                marks = json.load(f)
        todo = [it["id"] for it in items
                if it["id"] not in marks or time.time() - marks[it["id"]]["t"] > 7 * 86400][:200]
        for vid in todo:
            prefix = __import__("hashlib").sha256(vid.encode()).hexdigest()[:4]
            url = "https://sponsor.ajay.app/api/skipSegments/%s?categories=%s" % (
                prefix, urllib.parse.quote(json.dumps(["intro"])))
            try:
                data = json.loads(http_get(url, timeout=15).decode("utf-8"))
            except urllib.error.HTTPError as e:
                data = [] if e.code == 404 else None      # 404 = nothing marked for these videos
            except Exception:
                data = None
            if data is None:
                break                                    # SponsorBlock unreachable: try again next refresh
            segs = next((d["segments"] for d in data if d.get("videoID") == vid), [])
            marks[vid] = {"t": time.time(), "at": sorted(s["segment"][0] for s in segs)}
            time.sleep(0.2)
        if todo and path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(marks, f)
        return marks

    def _split_at_marks(self, cid, items):
        """Split episodes into parts at their segment changes, so commercials can go in between."""
        marks = self._break_marks(cid, items)
        out = []
        for it in items:
            d = it["duration"]
            cuts = []
            for s in (marks.get(it["id"]) or {}).get("at", []):
                s = int(s)
                if 120 <= s <= d - 180 and (not cuts or s - cuts[-1] >= 300):
                    cuts.append(s)
            edges = [0] + cuts + [d]
            for i in range(len(edges) - 1):
                part = dict(it, duration=edges[i + 1] - edges[i])
                if edges[i]:
                    part["inS"] = edges[i]
                if i < len(edges) - 2:
                    part["cont"] = True
                out.append(part)
        return out

    def _custom_versions(self, ch, items, here):
        """Like Spelunkr, a changed playlist becomes a new version that takes over at the next show."""
        cid = ch["id"]
        state_file = os.path.join(CACHE_DIR, "custom__%s__versions.json" % cid) if CACHE_DIR else None
        state = {"versions": []}
        if state_file and os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                state = json.load(f)
        vs = state["versions"]
        fingerprint = [(it["id"], it["duration"]) for it in items]

        def track(t):
            t = dict(t)
            if t.get("file") and not os.path.isabs(t["file"]):
                # next to channels.json, else next to the script (the bundled music folder)
                cands = [os.path.join(d, t["file"]) for d in (here, os.path.dirname(os.path.abspath(__file__)))]
                t["file"] = next((p for p in cands if os.path.exists(p)), cands[0])
            return t
        pool = self._ad_pool(ch["ads"]) if ch.get("ads") else []
        cfg_key = json.dumps(ch, sort_keys=True) + str(len(pool))

        def build(v):
            key = (cid, v["v"], v["epochMs"], v.get("startIndex", 0), cfg_key)
            if key not in self._vcache:
                card_ms = int(float(ch.get("card_seconds") or 0) * 1000)
                stretch = (lambda bs: stretch_bumps(bs, card_ms)) if card_ms > 0 else (lambda bs: bs)
                manifest = {"engine": 5, "items": v["items"], "bumps": stretch(ch.get("bumps") or []),
                            "audio": [track(t) for t in ch.get("music") or []],
                            "ads": {"slices": pool,
                                    **({"gapMs": ch["ad_gap_minutes"] * 60000} if is_int(ch.get("ad_gap_minutes")) else {}),
                                    **({"midBreakMs": ch["mid_break_minutes"] * 60000}
                                       if is_int(ch.get("mid_break_minutes")) else {})} if pool else None}
                platform = {"bumps": stretch(ch.get("network_bumps") or []), "audio": [track(t) for t in ch.get("network_music") or []]}
                self._vcache[key] = Version(cid, v, manifest, None, platform)
            return self._vcache[key]

        fp = lambda v: [(it["id"], it["duration"]) for it in v["items"]]
        changed = False
        if not vs:
            t = (now_ms() // 1000) * 1000
            vs.append({"v": 1, "epochMs": t, "effectiveAtMs": t, "startIndex": 0, "items": items})
            changed = True
        elif fp(vs[-1]) != fingerprint:
            now = now_ms()
            while len(vs) > 1 and vs[-1]["effectiveAtMs"] > now:   # a lineup that hasn't started yet: replace it
                vs.pop()
                changed = True
        if vs and fp(vs[-1]) != fingerprint:
            old = build(vs[-1])
            now = now_ms()
            keep = {it["id"] for it in items}
            e, start = old.locate(now)
            airing = old.items[e["videoIndex"]]["id"] if e["type"] == "video" else None
            if airing and airing not in keep:
                # what's on right now was dropped (e.g. it turned out to be unplayable): move on in 30 seconds
                start = now + 30_000
                n = len(old.items)
                old_id = next((old.items[(e["videoIndex"] + k) % n]["id"] for k in range(1, n + 1)
                               if old.items[(e["videoIndex"] + k) % n]["id"] in keep), items[0]["id"])
            else:
                t = now + 10 * 60_000                # otherwise switch over at the start of the next show
                for _ in range(5000):
                    e, start = old.locate(t)
                    if e["type"] == "video" and start >= t:
                        break
                    t = start + e["dur"]
                old_id = old.items[e["videoIndex"]]["id"]
            start_index = next((i for i, it in enumerate(items) if it["id"] == old_id), 0)
            state["last_v"] = max(state.get("last_v", 0), max(v["v"] for v in vs)) + 1   # numbers are never reused
            vs.append({"v": state["last_v"], "epochMs": start, "effectiveAtMs": start,
                       "startIndex": start_index, "items": items})
            del vs[:-3]
            changed = True
            log.info("%s: playlist changed - new lineup takes over at %s", cid,
                     time.strftime("%H:%M", time.localtime(start / 1000)))
        if changed and state_file:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(state, f)
        return [build(v) for v in vs]

    def _add_custom(self, chans, vers):
        if not self.custom_file or not os.path.exists(self.custom_file):
            return
        try:
            with open(self.custom_file, encoding="utf-8-sig") as f:   # tolerate Notepad's BOM
                cfg = json.load(f)
        except Exception as e:
            log.warning("%s: %s", os.path.basename(self.custom_file), e)
            return
        here = os.path.dirname(os.path.abspath(self.custom_file))
        for ch in cfg.get("channels", []):
            cid = ch.get("id")
            if not cid or (self.only and cid not in self.only) or cid in self.skip:
                continue
            try:
                if not ch.get("youtube"):
                    raise ValueError("needs a youtube playlist")
                items = self._youtube_items(ch)
                self._load_descriptions()
                if self.fetch_descriptions:
                    self._fetch_descriptions([it["id"] for it in items])
                if ch["youtube"].get("breaks") == "sponsorblock":
                    items = self._split_at_marks(cid, items)
                vl = self._custom_versions(ch, items, here)
            except Exception as e:
                log.warning("%s: skipped (%s)", cid, e)
                continue
            info = {k: ch[k] for k in ("id", "name", "dial", "color", "description", "family", "logomark", "size", "fit", "vhs")
                    if k in ch}
            info["custom"] = True
            chans.append(info)
            vers[cid] = vl

    def channel(self, cid):
        with self.lock:
            return next((c for c in self.channels if c["id"] == cid), None)

    def entry_at(self, cid, t):
        """(version, entry, start_ms, end_ms) on air at t. Cut short where a newer version takes over."""
        with self.lock:
            vl = self.versions[cid]
        ver = None
        for x in vl:
            if x.effective <= t:
                ver = x
        if ver is None:
            ver = min(vl, key=lambda x: (x.effective, -x.v))
        e, start = ver.locate(t)
        end = start + e["dur"]
        nxt = min((x.effective for x in vl if x.v > ver.v and x.effective > t), default=None)
        if nxt is not None and nxt < end:
            end = nxt
        return ver, e, start, end

    def upcoming_videos(self, cid, t, n=2):
        out = []
        while len(out) < n:
            ver, e, start, end = self.entry_at(cid, t)
            if e["type"] == "video" and start >= t:
                out.append((start, ver.items[e["videoIndex"]]))
            t = end
        return out

    def item_url(self, it):
        name = it.get("mp4") or it.get("file")
        if name:
            return archive_url(it["id"], name)
        ident = it["id"]
        if ident not in self._urls:
            files = fetch_json("https://archive.org/metadata/%s" % ident).get("files", [])
            for test in (lambda f: f.get("format") == "h.264 IA", lambda f: f.get("format") == "h.264",
                         lambda f: f.get("format") == "512Kb MPEG4", lambda f: f.get("format") == "MPEG4",
                         lambda f: str(f.get("name", "")).endswith(".mp4")):
                f = next((f for f in files if test(f)), None)
                if f:
                    self._urls[ident] = archive_url(ident, f["name"])
                    break
            else:
                raise RuntimeError("no video file found for %s" % ident)
        return self._urls[ident]

    def audio_url(self, track):
        if not track:
            return None
        if track.get("file"):                    # a local music file (custom channels)
            return track["file"] if os.path.exists(track["file"]) else None
        if track.get("filename"):
            return AUDIO_BASE + urllib.parse.quote(track["filename"], safe="")
        ident = track.get("id")
        if not ident:
            return None
        key = "audio:" + ident
        if key not in self._urls:
            try:
                files = fetch_json("https://archive.org/metadata/%s" % ident).get("files", [])
                f = (next((f for f in files if f.get("format") == "VBR MP3"), None)
                     or next((f for f in files if f.get("format") == "128Kbps MP3"), None)
                     or next((f for f in files if str(f.get("name", "")).endswith(".mp3")), None)
                     or next((f for f in files if f.get("format") == "Ogg Vorbis"), None))
                self._urls[key] = archive_url(ident, f["name"]) if f else None
            except Exception:
                return None
        return self._urls[key]

    def programmes(self, cid, t0, t1):
        """Guide entries: each show's slot runs until the next show starts (bumps and ads included)."""
        out, t = [], t0
        while t < t1:
            ver, e, start, end = self.entry_at(cid, t)
            prev = out[-1] if out else None
            if (e["type"] == "video" and prev and prev["item"] is not None and prev["ver"] is ver
                    and ver.items[prev["item"]].get("cont") and prev["stop"] == start
                    and (ver.items[prev["item"]].get("prog") or ver.items[prev["item"]]["id"])
                    == (ver.items[e["videoIndex"]].get("prog") or ver.items[e["videoIndex"]]["id"])):
                prev["stop"] = end          # the rest of an episode after a mid-show commercial break
            elif e["type"] == "video":
                out.append({"start": start, "stop": end, "ver": ver, "item": e["videoIndex"]})
            elif out and out[-1]["stop"] == start:
                out[-1]["stop"] = end
            else:
                out.append({"start": start, "stop": end, "ver": ver, "item": None})
            t = end
        return out


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------

RETRO_FONTS = ["star4000.ttf", "Star4000.ttf"]                  # the site's bump-card font
RETRO_SMALL_FONTS = ["star4000-large.ttf", "Star4000-Large.ttf"]  # the site's font for small print
PLAIN_FONTS = [
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/ttf-dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def find_font(*lists):
    here = os.path.dirname(os.path.abspath(__file__))
    for lst in lists:
        for p in lst:
            for cand in (p, os.path.join(here, p)):
                if cand and os.path.isfile(cand):
                    return os.path.abspath(cand)
    return None


RETRO_SWAPS = str.maketrans({"—": "-", "–": "-", "·": "/", "♫": "", "•": "/"})


def needs_plain_font(text):
    """The retro font only covers Latin text; CJK and the like use the plain font."""
    return any(ord(c) > 0x2FFF for c in text)


def ff_path(p):
    return "'%s'" % p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def ff_color(c):
    return "0x" + c.lstrip("#") if c and c.startswith("#") else "black"


def wrap_text(text, width):
    """Wrap to at most `width` characters per line, with lines as even as possible (like CSS text-wrap: balance)."""
    words = text.split()
    if not words:
        return []

    def greedy(limit):
        lines, cur = [], ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > limit:
                lines.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        return lines + [cur] if cur else lines
    fewest = len(greedy(width))
    for limit in range(max(1, math.ceil(len(text) / fewest)), width + 1):
        lines = greedy(limit)
        if len(lines) <= fewest:
            return lines
    return greedy(width)


def bump_timeline(e):
    """Mirror of the site's bump layout: when each slide shows and when each phase starts."""
    half = e.get("half")
    has_open, has_close = half != "close", half != "open"
    t = INTRO_MS / 1000 if has_open else 0.0
    intro_end = t
    entries = []
    if has_open:
        for i, s in enumerate(e["channelBump"]["slides"]):
            d = slide_dur(s) / 1000 + (TAIL_MS / 1000 if i == 0 else 0)
            entries.append(("channel", s["text"], t, t + d))
            t += d
    channel_end = t
    if has_open:
        t += PAUSE_MS / 1000
    gap_start = t
    if has_open and has_close:
        t += GAP_MS / 1000
    platform_start = t
    if has_close:
        for i, s in enumerate(e["platformBump"]["slides"]):
            d = slide_dur(s) / 1000 + (TAIL_MS / 1000 if i == 0 else 0)
            entries.append(("platform", s["text"], t, t + d))
            t += d
    if has_close:
        t += PAUSE_MS / 1000
    return {"entries": entries, "intro_end": intro_end, "channel_end": channel_end, "gap_start": gap_start,
            "platform_start": platform_start, "total": t}


def attribution(track):
    if not track or not track.get("artist"):
        return None
    lic = track.get("license")
    if lic in ("public-domain", "pd"):
        lic = " · Public Domain"
    elif lic == "cc0":
        lic = " · CC0"
    elif lic:
        lic = " · CC %s %s · Clip used" % (lic.upper(), track.get("license_version") or "4.0")
    else:
        lic = ""
    if track.get("source"):      # longer credit: split over two lines so it stays readable
        return ["♫ MUSIC: \"%s\" — %s" % (track.get("title") or "Untitled", track["artist"]),
                track["source"] + lic]
    return ["♫ MUSIC: \"%s\" — %s%s" % (track.get("title") or "Untitled", track["artist"], lic)]


TS_GAP = 0.12   # seconds of spacing between segments, so timestamps never step backwards at a hand-off
                # (covers the encoder's frame reordering and the AAC encoder's start-up delay)


class _LiveChannel:
    """One channel on the air: a single encode, fanned out to every viewer. Keeps running for
    IDLE_SECONDS after the last viewer leaves, so flipping away and back is instant."""
    IDLE_SECONDS = 30
    TS = 188

    def __init__(self, streamer, cid):
        self.cid = cid
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.subs = {}                 # viewer queue -> bytes still to skip to land on a packet boundary
        self.pos = 0                   # bytes broadcast so far
        self.idle_timer = None
        self.remux = subprocess.Popen(
            [streamer.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
             "-probesize", "200000", "-analyzeduration", "500000",
             "-f", "mpegts", "-i", "pipe:0", "-map", "0", "-c", "copy",
             "-mpegts_flags", "+resend_headers", "-pat_period", "0.5", "-f", "mpegts", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, **NO_WINDOW)
        threading.Thread(target=self._fan_out, daemon=True).start()
        threading.Thread(target=self._produce, args=(streamer,), daemon=True).start()
        log.info("[%s] on the air", cid)

    def _produce(self, streamer):
        try:
            streamer._run_channel(self.cid, self.remux.stdin, self.stop)
        except Exception as e:
            log.warning("[%s] stopped: %s", self.cid, e)
        finally:
            self.close()

    def _fan_out(self):
        try:
            while True:
                chunk = self.remux.stdout.read(65536)
                if not chunk:
                    break
                with self.lock:
                    for q, skip in list(self.subs.items()):
                        data = chunk
                        if skip is None:                    # new viewer: start on a TS packet boundary
                            data = chunk[(-self.pos) % self.TS:]
                            self.subs[q] = 0
                        try:
                            q.put_nowait(data)
                        except queue.Full:                  # a viewer that stopped reading: drop it
                            del self.subs[q]
                            q.put(None)
                    self.pos += len(chunk)
        finally:
            self.close()

    def subscribe(self):
        q = queue.Queue(maxsize=600)
        with self.lock:
            self.subs[q] = None
            if self.idle_timer:
                self.idle_timer.cancel()
                self.idle_timer = None
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subs.pop(q, None)
            if not self.subs and not self.stop.is_set():
                self.idle_timer = threading.Timer(self.IDLE_SECONDS, self._idle_check)
                self.idle_timer.daemon = True
                self.idle_timer.start()

    def _idle_check(self):
        with self.lock:
            if self.subs:
                return
        log.info("[%s] no viewers for %ds, going off the air", self.cid, self.IDLE_SECONDS)
        self.close()

    def viewers(self):
        with self.lock:
            return len(self.subs)

    def close(self):
        if self.stop.is_set():
            return
        self.stop.set()
        try:
            self.remux.kill()
        except OSError:
            pass
        with self.lock:
            for q in self.subs:
                try:
                    q.put_nowait(None)
                except queue.Full:
                    pass
            self.subs.clear()


class _Pending:
    """A segment being started in the background, so slow lookups never hold up the picture."""

    def __init__(self, start):
        self.result = None
        self.thread = threading.Thread(target=self._run, args=(start,), daemon=True)
        self.thread.start()

    def _run(self, start):
        try:
            self.result = start()
        except Exception as e:
            log.warning("could not start the next segment: %s", e)

    def get(self):
        self.thread.join()
        return self.result

    def kill(self):
        if self.get():
            self.result.kill()


class _Segment:
    """One running ffmpeg, its output collected in a queue so it can start before it's needed."""

    def __init__(self, cmd, tmp, sched, dur, ts, kind):
        self.cmd, self.tmp, self.sched, self.dur, self.ts, self.kind = cmd, tmp, sched, dur, ts, kind
        self.q = queue.Queue()
        self.errors = []
        self.first = None
        self.sent = 0
        self.launched = time.monotonic()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL, cwd=tmp, **NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain, daemon=True).start()

    def _read(self):
        try:
            while True:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                if self.first is None:
                    self.first = time.monotonic()
                self.sent += len(chunk)
                self.q.put(chunk)
        finally:
            self.q.put(None)

    def _drain(self):
        for line in self.proc.stderr:
            self.errors.append(line.decode("utf-8", "replace").rstrip())
            del self.errors[:-20]

    def cleanup(self):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
            self.tmp = None

    def kill(self):
        try:
            self.proc.kill()
            self.proc.wait()
        except OSError:
            pass
        self.cleanup()


class Streamer:
    def __init__(self, catalog, ffmpeg, ffprobe, width, height, preset, maxrate, fonts, lead=0.0,
                 hw=None, bug=True, static=True, vhs_cards=True):
        self.catalog = catalog
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.w, self.h = width, height
        self.preset, self.maxrate = preset, maxrate
        self.font, self.font_small, self.font_plain = fonts
        self.lead = lead                               # extra seconds ahead, to cancel out player buffering
        self.hw = hw                                   # e.g. ("qsv", "/dev/dri/renderD129") or ("nvenc", None)
        self.bug = bug                                 # small channel logo in the corner during shows
        self.static = static                           # TV snow while a channel tunes in
        self._live = {}                                # channel id -> _LiveChannel shared by its viewers
        self._live_lock = threading.Lock()
        self.startup = {"tape": 4.0, "card": 1.0, "youtube": 6.0}   # learned: seconds from launch to first output
        self._silent = set()                           # tapes found to have no audio track
        self._logos = {}
        self.crop = False                              # fill the screen instead of letterboxing
        # where to fake a worn VHS look: "shows", "cards", "ads". Cards on every channel unless turned off;
        # custom channels can add more with "vhs" in channels.json.
        self.vhs = self._vhs_base = frozenset(["cards"] if vhs_cards else [])
        self._yt = {}                                  # video id -> (good until, [stream urls])
        self._url_headers = {}                         # stream url -> the HTTP headers yt-dlp used for it
        self._yt_lock = threading.Lock()

    def sized(self, channel):
        """This streamer with a channel's own picture settings: "size" (e.g. 854x480) and
        "fit" ("letterbox" keeps the whole frame, "crop" fills the screen like pan & scan)."""
        channel = channel or {}
        w, h = self.w, self.h
        if channel.get("size"):
            w, h = (int(x) for x in str(channel["size"]).lower().split("x"))
        crop = channel.get("fit") == "crop"
        vhs = channel.get("vhs") or []
        vhs = self._vhs_base | frozenset(["shows", "cards", "ads"] if vhs is True else [vhs] if isinstance(vhs, str) else vhs)
        if (w, h, crop, vhs) == (self.w, self.h, self.crop, self.vhs):
            return self
        s = copy.copy(self)          # shares the learned startup times and caches
        s.w, s.h, s.crop, s.vhs = w, h, crop, vhs
        return s

    def _vhs_graph(self, src, dst):
        """A worn, badly tracked VHS dub: soft smeared detail, colour bleeding to the right, lifted
        blacks, tape grain, head-switching noise along the bottom edge, and now and then a tracking
        glitch rolling up the picture. CPU filters at SD size, so they're cheap even with GPU encoding."""
        w, h = self.w, self.h
        band = max(8, h // 48)
        return ("[%s]scale=%d:%d:flags=bilinear,scale=%d:%d:flags=bicubic,"           # ~250 lines of detail
                "chromashift=cbh=3:crh=4:edge=smear,"                                  # colour bleed
                "boxblur=luma_radius=0:luma_power=0:chroma_radius=2:chroma_power=2,"
                "eq=contrast=0.9:brightness=0.03:saturation=1.18:gamma=1.04,"           # milky blacks, hot colour
                "colorbalance=rs=0.01:bs=0.01:rm=0.02:bm=-0.02,"
                "noise=c0s=10:c0f=t+u:c1s=5:c1f=t:c2s=5:c2f=t,"                         # tape grain
                "split[vh_a][vh_b];"
                "[vh_b]crop=w=iw:h=%d:x=0:y=ih-%d,noise=c0s=45:c0f=t[vh_c];"            # head-switching noise
                "[vh_a][vh_c]overlay=x=0:y=H-%d,split[vh_e][vh_f];"                     # (kept square)
                "[vh_f]crop=w=iw-10:h=%d:x=0:y='mod(t*120,ih-%d)',noise=c0s=60:c0f=t,"  # tracking glitch band
                "eq=brightness=0.08[vh_g];"
                "[vh_e][vh_g]overlay=x=10:y='mod(t*120,H-%d)':enable='lt(mod(t,41),0.8)'[%s]"
                % (src, w * 3 // 5, h, w, h, band, band, band, band * 3, band * 3, band * 3, dst))

    # -- helpers ----------------------------------------------------------------

    def has_audio(self, url):
        return url not in self._silent

    def youtube_urls(self, vid):
        """Direct stream addresses for a YouTube video (they work for a few hours, so they're reused)."""
        with self._yt_lock:
            hit = self._yt.get(vid)
            if hit and time.time() < hit[0]:
                return hit[1]
        cmd = [PYTHON, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
               "-f", "bv*[height<=%d][vcodec^=avc1]+ba[ext=m4a]/b[height<=%d]/b" % (self.h, self.h), "-j"]
        if shutil.which("node"):
            cmd += ["--js-runtimes", "node"]
        out = subprocess.run(cmd + ["https://www.youtube.com/watch?v=" + vid],
                             capture_output=True, text=True, encoding="utf-8", timeout=90, **NO_WINDOW)
        try:
            info = json.loads(out.stdout) if out.returncode == 0 else {}
        except ValueError:
            info = {}
        fmts = info.get("requested_formats") or ([info] if info.get("url") else [])
        urls = [f["url"] for f in fmts if str(f.get("url", "")).startswith("http")][:2]
        if not urls:
            raise RuntimeError("yt-dlp: " + ((out.stderr or "").strip().splitlines() or ["no stream found"])[-1])
        # YouTube can refuse links opened with different headers than the ones yt-dlp used to get them
        with self._yt_lock:
            for f in fmts[:2]:
                if f.get("http_headers"):
                    self._url_headers[f["url"]] = f["http_headers"]
        m = re.search(r"[?&/]expire[=/](\d+)", urls[0])
        good_until = int(m.group(1)) - 1800 if m else time.time() + 3600
        with self._yt_lock:
            self._yt[vid] = (good_until, urls)
        return urls

    def forget_youtube_urls(self, urls):
        """Drop cached stream links (e.g. after YouTube refused them twice) so the next try gets fresh ones."""
        with self._yt_lock:
            for vid in [v for v, (_, u) in self._yt.items() if set(u) & set(urls)]:
                for x in self._yt.pop(vid)[1]:
                    self._url_headers.pop(x, None)

    def _head(self):
        return [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"] + hw_args(self.hw)

    def _tail(self):
        """End of every video filter chain: hand frames to the GPU when encoding there."""
        return hw_tail(self.hw)

    def _out(self, ts_offset):
        rate = int(self.maxrate)
        if self.hw and self.hw[0] == "nvenc":
            video = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-b:v", "%dk" % int(rate * 0.75),
                     "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2), "-g", "60"]
        elif self.hw and self.hw[0] == "amf":
            video = ["-c:v", "h264_amf", "-quality", "speed", "-rc", "vbr_peak", "-b:v", "%dk" % int(rate * 0.75),
                     "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2), "-g", "60"]
        elif self.hw and self.hw[0] == "qsv":
            video = ["-c:v", "h264_qsv", "-preset", "faster", "-b:v", "%dk" % int(rate * 0.75),
                     "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2), "-g", "60"]
        elif self.hw:
            video = ["-c:v", "h264_vaapi", "-rc_mode", "VBR", "-b:v", "%dk" % int(rate * 0.75),
                     "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2), "-g", "60"]
        else:
            video = ["-c:v", "libx264", "-preset", self.preset, "-crf", "23",
                     "-maxrate", "%dk" % rate, "-bufsize", "%dk" % (rate * 2), "-g", "60", "-pix_fmt", "yuv420p"]
        return video + ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
                        "-max_muxing_queue_size", "1024",
                        "-output_ts_offset", "%.3f" % ts_offset,
                        "-mpegts_flags", "+resend_headers", "-f", "mpegts", "pipe:1"]

    def _fit(self, tail=True):
        w, h = self.w, self.h
        if self.crop:
            f = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                 "setsar=1,fps=30000/1001,format=yuv420p" % (w, h, w, h))
        else:
            f = ("scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2,"
                 "setsar=1,fps=30000/1001,format=yuv420p" % (w, h, w, h))
        return f + (self._tail() if tail else "")

    def _net_in(self, url, seek, realtime=True):
        net = []
        if url.startswith("http"):
            hdrs = dict(self._url_headers.get(url) or {})
            ua = hdrs.pop("User-Agent", None) or USER_AGENT
            net = ["-user_agent", ua, "-rw_timeout", "20000000",
                   "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
            if hdrs:
                net += ["-headers", "".join("%s: %s\r\n" % kv for kv in hdrs.items())]
        return net + ["-ss", "%.3f" % max(0.0, seek)] + (["-re"] if realtime else []) + ["-i", url]

    def _logo_file(self, channel, tmp):
        p = os.path.join(tmp, "logo.png")
        with open(p, "wb") as f:
            f.write(render_logo(channel, scale=4, pad=0))
        return p

    def _text(self, tmp, name, text, size, color, x, y, start, end, border=3, small=False):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        ff = self.font_plain if needs_plain_font(text) else ((self.font_small if small else None) or self.font)
        ff = ff or self.font_plain
        if ff and ff != self.font_plain:
            # the retro fonts have no glyphs for these, so they'd come out blank
            text = text.translate(RETRO_SWAPS).replace("  ", " ").strip()
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        font = "fontfile=%s:" % ff_path(ff) if ff else ""
        return ("drawtext=%stextfile=%s:expansion=none:fontsize=%d:fontcolor=%s:borderw=%d:bordercolor=black:"
                "y_align=font:x=%s:y=%s:enable='between(t,%.3f,%.3f)'"
                % (font, ff_path(path), size, ff_color(color), border, x, y, start, end))

    # -- one ffmpeg command per kind of segment ---------------------------------

    def clip_cmd(self, url, seek, duration, ts_offset, gain_db=0, audio_url=None, bug=None, vhs=False):
        """A tape or video. `bug` is a logo image to show faintly in the corner (during shows);
        `vhs` runs the picture (logo included, like a taped broadcast) through the worn-tape look."""
        audio = self.has_audio(url)
        cmd = self._head() + self._net_in(url, seek)
        n = 1
        if audio_url:                    # YouTube serves picture and sound separately
            cmd += self._net_in(audio_url, seek)
            amap, af, n = "1:a:0", "aresample=48000:async=1,aformat=channel_layouts=stereo", 2
        elif audio:
            amap, af = "0:a:0", "aresample=48000:async=1,aformat=channel_layouts=stereo"
            if gain_db:
                af += ",volume=%.1fdB" % gain_db
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
            amap, af, n = "1:a:0", None, 2
        if bug:
            size, margin = max(32, self.h // 9), max(10, self.h // 32)
            cmd += ["-loop", "1", "-i", bug]
            graph = ("[0:v:0]%s[base];[%d:v]format=rgba,scale=%d:%d:flags=neighbor,colorchannelmixer=aa=0.55[bug];"
                     "[base][bug]overlay=W-w-%d:H-h-%d:shortest=1,format=yuv420p[pre]"
                     % (self._fit(tail=False), n, size, size, margin, margin))
            if vhs:
                graph += ";" + self._vhs_graph("pre", "post") + ";[post]null%s[vout]" % self._tail()
            else:
                graph += ";[pre]null%s[vout]" % self._tail()
            cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", amap]
        elif vhs:
            graph = ("[0:v:0]%s[pre];" % self._fit(tail=False) + self._vhs_graph("pre", "post")
                     + ";[post]null%s[vout]" % self._tail())
            cmd += ["-filter_complex", graph, "-map", "[vout]", "-map", amap]
        else:
            cmd += ["-map", "0:v:0", "-map", amap, "-vf", self._fit()]
        if af:
            cmd += ["-af", af]
        return cmd + ["-t", "%.3f" % duration] + self._out(ts_offset)

    def card_cmd(self, tmp, duration, offset, ts_offset, bg, video_filters, overlays, audio_parts):
        """Generic card: a coloured background with drawn text/boxes, optional logo overlays and music beds.
        Times in video_filters/overlays/audio_parts are seconds from the start of the card; `offset` is how far
        into the card we are joining. Video timestamps are shifted by `offset` so those times line up.
        audio_parts: (url, seek seconds into the file at the moment it is joined, start, end, volume)."""
        cmd = self._head() + [
               "-re", "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=30000/1001:d=%.3f" % (ff_color(bg), self.w, self.h, duration + 1),
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        n = 2
        graph = ["[0:v]setpts=PTS+%.3f/TB%s[v0]" % (offset, "".join("," + f for f in video_filters))]
        last = "v0"
        for i, (path, x, y, a, b) in enumerate(overlays):
            cmd += ["-loop", "1", "-i", path]
            graph.append("[%s][%d:v]overlay=x=%s:y=%s:enable='between(t,%.3f,%.3f)'[v%d]" % (last, n, x, y, a, b, i + 1))
            last = "v%d" % (i + 1)
            n += 1
        if "cards" in self.vhs:
            graph.append("[%s]setpts=PTS-STARTPTS,format=yuv420p[pre]" % last)
            graph.append(self._vhs_graph("pre", "post"))
            graph.append("[post]null%s[vout]" % self._tail())
        else:
            graph.append("[%s]setpts=PTS-STARTPTS,format=yuv420p%s[vout]" % (last, self._tail()))
        mix = ["[1:a]atrim=0:%.3f[a0]" % (duration + 0.5)]
        labels = ["[a0]"]
        fade = FADE_MS / 1000
        for j, (url, seek, a, b, vol) in enumerate(audio_parts):
            start = max(a, offset)
            if b - start < 0.05:
                continue
            cmd += self._net_in(url, seek, realtime=False)
            length = b - start
            fin = fade if a >= offset else 0.3
            mix.append("[%d:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:%.3f,asetpts=PTS-STARTPTS,"
                       "afade=t=in:d=%.3f,afade=t=out:st=%.3f:d=%.3f,volume=%.2f,adelay=%d:all=1[m%d]"
                       % (n, length, min(fin, length / 2), max(0, length - fade), min(fade, length / 2), vol,
                          int((start - offset) * 1000), j))
            labels.append("[m%d]" % j)
            n += 1
        mix.append("%samix=inputs=%d:duration=first:normalize=0[aout]" % ("".join(labels), len(labels)))
        cmd += ["-filter_complex", ";".join(graph + mix), "-map", "[vout]", "-map", "[aout]",
                "-t", "%.3f" % duration]
        return cmd + self._out(ts_offset)

    def bump_cmd(self, tmp, channel, e, offset, duration, ts_offset):
        tl = bump_timeline(e)
        color = channel.get("color") or GOLD
        W, H = self.w, self.h
        size = max(18, H // 15)
        filters, overlays, audio = [], [], []
        if tl["intro_end"] > 0:
            filters.append("drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,0,%.3f)'" % tl["gap_start"])
            logo = self._logo_file(channel, tmp)
            overlays.append((logo, "(W-w)/2", "(H-h)/2-%d" % (size), 0, tl["intro_end"]))
            filters.append(self._text(tmp, "intro.txt", "CH %s · %s" % (channel.get("dial", ""), channel["name"]),
                                      max(14, size * 2 // 3), color, "(w-text_w)/2", "h/2+%d" % (size * 2), 0, tl["intro_end"]))
            for k in range(4):
                filters.append("drawbox=x=%d:y=%d:w=iw-%d:h=ih-%d:color=%s@%.2f:t=2:enable='between(t,%.3f,%.3f)'"
                               % (k * 2, k * 2, k * 4, k * 4, ff_color(color), [1, .72, .48, .26][k], tl["intro_end"], tl["channel_end"]))
        if tl["platform_start"] < tl["total"]:
            for k, c in enumerate(PLATFORM_BORDER):
                filters.append("drawbox=x=%d:y=%d:w=iw-%d:h=ih-%d:color=%s:t=2:enable='between(t,%.3f,%.3f)'"
                               % (k * 2, k * 2, k * 4, k * 4, ff_color(c), tl["platform_start"] - 0.001, tl["total"]))
        for i, (phase, text, a, b) in enumerate(tl["entries"]):
            lines = wrap_text(self._tag_text(text).lower(), max(12, int(W * 0.85 / (size * 0.56))))
            top = (H - len(lines) * size * 1.35) / 2
            for j, line in enumerate(lines):
                filters.append(self._text(tmp, "s%d_%d.txt" % (i, j), line, size, color if phase == "channel" else GOLD,
                                          "(w-text_w)/2", "%d" % (top + j * size * 1.35), a, b))
        cat = self.catalog
        if e.get("channelAudio"):
            url = cat.audio_url(e["channelAudio"])
            if url:
                start = e["channelAudio"].get("startOffset", 0) + max(0.0, offset - tl["intro_end"])
                audio.append((url, start, tl["intro_end"], tl["gap_start"], 0.3))
            filters += self._credits(tmp, "c", attribution(e["channelAudio"]), size, GOLD, tl["intro_end"], tl["channel_end"])
        if e.get("platformAudio"):
            url = cat.audio_url(e["platformAudio"])
            if url:
                start = e["platformAudio"].get("startOffset", 0) + max(0.0, offset - tl["platform_start"])
                audio.append((url, start, tl["platform_start"], tl["total"], 0.3))
            filters += self._credits(tmp, "p", attribution(e["platformAudio"]), size, "#aab4d8",
                                     tl["platform_start"], tl["total"])
        return self.card_cmd(tmp, duration, offset, ts_offset, PLATFORM_BG, filters, overlays, audio)

    def _credits(self, tmp, tag, lines, size, color, start, end):
        """The music credit in the bottom-left corner (one or two lines, same size)."""
        if not lines:
            return []
        fs = min(self._credit_size(line, size) for line in lines)
        out = []
        for i, line in enumerate(lines):
            y = "h*0.92" if i == len(lines) - 1 else "h*0.92-%d" % int(fs * 1.4 * (len(lines) - 1 - i))
            out.append(self._text(tmp, "credit_%s%d.txt" % (tag, i), line, fs, color, "w*0.04", y,
                                  start, end, border=2, small=True))
        return out

    def _credit_size(self, note, size):
        """Small print that still fits on one line (the retro font is roughly 0.6 em per character)."""
        return max(8, min(size // 2, int(self.w * 0.92 / (0.6 * max(1, js_len(note))))))

    def _tag_text(self, text):
        """Slides can mention channels as [channel-id]; show them as 'CH 12 HAPPY TREES'."""
        def sub(m):
            c = self.catalog.channel(m.group(1))
            return "CH %s %s" % (c.get("dial", ""), c["name"]) if c else m.group(0)
        return re.sub(r"\[([a-z0-9-]+)\]", sub, text)

    def upnext_cmd(self, tmp, channel, e, start_ms, offset, duration, ts_offset):
        color = channel.get("color") or GOLD
        H = self.h
        size = max(16, H // 18)
        nxt = self.catalog.upcoming_videos(channel["id"], start_ms + e["dur"] - 1, 2)
        fmt = lambda ms: time.strftime("%I:%M %p", time.localtime(ms / 1000)).lstrip("0")
        filters = []
        total = e["dur"] / 1000
        rows = [("UP NEXT ON %s" % channel["name"], color, size)]
        for label, (when, it) in zip(("NEXT", "LATER"), nxt):
            rows.append(("%s  %s" % (label, fmt(when)), GOLD, int(size * 0.8)))
            for line in wrap_text(it.get("title") or it["id"], 34)[:2]:
                rows.append((line, "#e8eaf0", size))
        y = H * 0.2
        for i, (text, col, sz) in enumerate(rows):
            filters.append(self._text(tmp, "u%d.txt" % i, text, sz, col, "w*0.1", "%d" % y, 0, total + 1))
            y += sz * 1.5
        for k in range(4):
            filters.append("drawbox=x=%d:y=%d:w=iw-%d:h=ih-%d:color=%s@%.2f:t=2"
                           % (k * 2, k * 2, k * 4, k * 4, ff_color(color), [1, .72, .48, .26][k]))
        audio = []
        sting = e.get("sting")
        if sting and sting.get("track"):
            url = AUDIO_BASE + urllib.parse.quote(sting["track"], safe="")
            audio.append((url, sting.get("startMs", 0) / 1000 + offset, 0, total, 0.5))
        return self.card_cmd(tmp, duration, offset, ts_offset, "#000000", filters, [], audio)

    def standby_cmd(self, duration, ts_offset):
        return self._head() + [
                "-re", "-f", "lavfi", "-i", "smptebars=size=%dx%d:rate=30000/1001" % (self.w, self.h),
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a:0", "-vf", self._fit(), "-t", "%.3f" % duration] + self._out(ts_offset)

    def static_cmd(self, channel, duration, tmp):
        """TV snow with a green on-screen channel number while the channel tunes in."""
        W, H = self.w, self.h
        osd = max(20, H // 11)
        name = (channel.get("name") or "").upper()
        texts = [self._text(tmp, "osd1.txt", "CH %s" % channel.get("dial", ""), osd, "#5dff5d",
                            "w-text_w-w*0.06", "h*0.07", 0, duration + 1, border=3),
                 self._text(tmp, "osd2.txt", name, max(12, osd // 2), "#5dff5d",
                            "w-text_w-w*0.06", "h*0.07+%d" % int(osd * 1.3), 0, duration + 1, border=2),
                 self._text(tmp, "osd3.txt", "ADJUSTING THE ANTENNA...", max(12, osd // 2), "#e8eaf0",
                            "(w-text_w)/2", "h*0.86", 0, duration + 1, border=2, small=True)]
        snow = ("noise=alls=100:allf=t+u,hue=s=0,eq=contrast=1.4:brightness=-0.05,boxblur=1:1,"
                + ",".join(texts) + ",format=yuv420p" + self._tail())
        fade = max(0.0, duration - 0.25)
        return self._head() + [
            "-re", "-f", "lavfi", "-i", "color=c=0x808080:s=%dx%d:r=30000/1001:d=%.3f" % (W, H, duration + 0.5),
            "-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.06:sample_rate=48000",
            "-map", "0:v:0", "-map", "1:a:0", "-vf", snow,
            "-af", "highpass=f=300,lowpass=f=7000,aformat=channel_layouts=stereo,afade=t=out:st=%.3f:d=0.25" % fade,
            "-t", "%.3f" % duration] + self._out(0.0)

    # -- the live loop ------------------------------------------------------------

    def describe(self, ver, e):
        t = e["type"]
        if t == "video":
            return ver.items[e["videoIndex"]].get("title") or ver.items[e["videoIndex"]]["id"]
        if t == "ad":
            return "commercial (%s)" % e["slice"].get("year", "?")
        if t == "up-next":
            return "Up Next card"
        return "bump break" + (" (%s)" % e["half"] if e.get("half") else "")

    def segment_cmd(self, cid, ver, e, start, t, remaining, ts_offset, tmp):
        offset = (t - start) / 1000
        kind = e["type"]
        if kind == "video":
            it = ver.items[e["videoIndex"]]
            seek = (it["inS"] if is_int(it.get("inS")) and it["inS"] > 0 else 0) + offset
            bug = self._logo_file(self.catalog.channel(cid) or {"id": cid}, tmp) if self.bug else None
            vhs = "shows" in self.vhs
            if it.get("youtube"):
                urls = self.youtube_urls(it["id"])
                return self.clip_cmd(urls[0], seek, remaining, ts_offset,
                                     audio_url=urls[1] if len(urls) > 1 else None, bug=bug, vhs=vhs)
            return self.clip_cmd(self.catalog.item_url(it), seek, remaining, ts_offset, bug=bug, vhs=vhs)
        if kind == "ad":
            s = e["slice"]
            return self.clip_cmd(archive_url(s["tape"], s["file"]), (s.get("inMs", 0)) / 1000 + offset,
                                 remaining, ts_offset, s.get("gainDb", 0), vhs="ads" in self.vhs)
        channel = self.catalog.channel(cid) or {"id": cid, "name": cid}
        if kind == "up-next":
            return self.upnext_cmd(tmp, channel, e, start, offset, remaining, ts_offset)
        return self.bump_cmd(tmp, channel, e, offset, remaining, ts_offset)

    def stream(self, cid, wfile):
        """Send a channel to one viewer. Every viewer of a channel shares the same running stream."""
        with self._live_lock:
            ch = self._live.get(cid)
            if ch is None or ch.stop.is_set():
                ch = _LiveChannel(self, cid)
                self._live[cid] = ch
            q = ch.subscribe()
        log.info("[%s] viewers: %d", cid, ch.viewers())
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            ch.unsubscribe(q)
            log.info("[%s] viewers: %d", cid, ch.viewers())

    def _run_channel(self, cid, sink, stop):
        self.sized(self.catalog.channel(cid))._segments(cid, sink, stop)

    @staticmethod
    def _kind(ver, e):
        if e["type"] == "video" and ver.items[e["videoIndex"]].get("youtube"):
            return "youtube"
        return "tape" if e["type"] in ("video", "ad") else "card"

    def _launch(self, cid, sched_ms, ts):
        """Start ffmpeg for whatever airs at schedule time sched_ms (None = live now).
        Starts a little ahead of the schedule, by how long ffmpeg usually takes to get going,
        so the first frame comes out right on time. Never lets the stream drift more than 2s behind."""
        if sched_ms is None:
            t = now_ms() + int(self.lead * 1000)
            ver, e, start, end = self.catalog.entry_at(cid, t)
            t += int(self.startup[self._kind(ver, e)] * 1000)
        else:
            t = sched_ms
        ver, e, start, end = self.catalog.entry_at(cid, t)
        if end - t < 700:                       # don't bother starting a sliver; take the next thing
            t = end
            ver, e, start, end = self.catalog.entry_at(cid, t)
        dur = (end - t) / 1000
        tmp = tempfile.mkdtemp(prefix="spelunkr-")
        try:
            cmd = self.segment_cmd(cid, ver, e, start, t, dur, ts, tmp)
        except Exception as ex:
            log.warning("[%s] could not start segment: %s", cid, ex)
            it = ver.items[e["videoIndex"]] if e["type"] == "video" else {}
            if it.get("youtube") and is_permanent_yt_error(str(ex)):
                self.catalog.mark_unplayable(it["id"], str(ex))
            dur = min(dur, 20.0)
            cmd = self.standby_cmd(dur, ts)
        log.info("[%s] %s  (+%ds, %ds)", cid, self.describe(ver, e), int((t - start) / 1000), int(dur))
        return _Segment(cmd, tmp, t, dur, ts, self._kind(ver, e))

    def _segments(self, cid, sink, stop):
        if self.static:
            # tune-in: TV snow plays while the real picture loads; the show starts right where the snow ends
            t = now_ms() + int(self.lead * 1000)
            ver, e, start, end = self.catalog.entry_at(cid, t)
            snow = min(max(self.startup[self._kind(ver, e)] + 1.0, 2.5), 12.0)
            tmp = tempfile.mkdtemp(prefix="spelunkr-")
            cmd = self.static_cmd(self.catalog.channel(cid) or {"id": cid, "name": cid}, snow, tmp)
            cur = _Segment(cmd, tmp, t, snow, 0.0, "card")
            sched = t + int(snow * 1000)
            nxt = _Pending(lambda: self._launch(cid, sched, snow + TS_GAP))
        else:
            cur = self._launch(cid, None, 0.0)
            nxt = None
        behind = None          # seconds the viewer's picture runs behind the schedule
        switched = time.monotonic()
        yt_retries = 0         # quick retries after YouTube refused a stream
        try:
            while not stop.is_set():
                measured = False
                # forward this segment; near its end, start the next one so it's warmed up in time
                while not stop.is_set():
                    try:
                        chunk = cur.q.get(timeout=0.2)
                    except queue.Empty:
                        chunk = b""
                    if chunk is None:
                        break
                    if chunk:
                        if not measured:
                            measured = True
                            if behind is None:      # tune-in: how late did the first picture arrive?
                                behind = time.time() - (cur.sched / 1000 - self.lead)
                            else:                   # a hand-off that wasn't ready in time adds to the delay
                                behind += max(0.0, time.monotonic() - switched)
                        try:
                            sink.write(chunk)
                        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, ValueError):
                            return
                    if nxt is None and cur.first is not None:
                        warmup = max(self.startup.values()) + 1.5
                        if time.monotonic() - cur.first >= cur.dur - warmup:
                            skip = behind if behind and behind > 0.75 else 0.0
                            if skip:                # catch up by starting the next segment that much later
                                log.info("[%s] catching up %.1fs", cid, skip)
                                behind -= skip
                            sched, ts = cur.sched + int((cur.dur + skip) * 1000), cur.ts + cur.dur + TS_GAP
                            nxt = _Pending(lambda s=sched, x=ts: self._launch(cid, s, x))
                if stop.is_set():
                    return
                code = cur.proc.wait()
                if cur.first is not None:
                    took = cur.first - cur.launched
                    self.startup[cur.kind] = round(0.7 * self.startup[cur.kind] + 0.3 * min(took, 20.0), 2)
                cur.cleanup()
                if code == 0 and cur.sent:
                    yt_retries = 0
                    cur, nxt = ((nxt.get() if nxt else None)
                                or self._launch(cid, cur.sched + int(cur.dur * 1000), cur.ts + cur.dur + TS_GAP)), None
                    switched = time.monotonic()
                    continue
                # this segment broke
                err = " | ".join(cur.errors[-3:]) or "(no output)"
                log.warning("[%s] ffmpeg exited %s: %s", cid, code,
                            re.sub(r"https://[^ ]*googlevideo\.com/\S+", "<YouTube stream>", err))
                played = (time.monotonic() - cur.first) if cur.first else 0.0
                if nxt:
                    nxt.kill()
                    nxt = None
                if cur.kind == "youtube" and "403" in err and yt_retries < 2:
                    # YouTube sometimes refuses the first request for a link that works moments later:
                    # try again right away; the second time, with freshly fetched links
                    yt_retries += 1
                    if yt_retries == 2:
                        self.forget_youtube_urls([a for a in cur.cmd if "googlevideo.com" in a])
                    log.info("[%s] YouTube refused the stream, trying again (%d)", cid, yt_retries)
                    time.sleep(0.5)
                    cur = self._launch(cid, None, cur.ts + played + TS_GAP)
                    switched = time.monotonic()
                    continue
                yt_retries = 0
                if "matches no streams" in err or "Stream map" in err:
                    url = next((cur.cmd[i + 1] for i, a in enumerate(cur.cmd[:-1]) if a == "-i" and cur.cmd[i + 1].startswith("http")), None)
                    if url and url not in self._silent:
                        self._silent.add(url)          # silent tape: retry right away with a quiet audio track
                        cur = self._launch(cid, None, cur.ts + played + TS_GAP)
                        switched = time.monotonic()
                        continue
                # colour bars for the rest of that tape (20s at most); the next launch rejoins the live schedule
                bars = min(max(cur.dur - played, 1.0), 20.0)
                cur = _Segment(self.standby_cmd(bars, cur.ts + played + TS_GAP), None,
                               cur.sched + int(played * 1000), bars, cur.ts + played + TS_GAP, "card")
                switched = time.monotonic()
        finally:
            for s in (cur, nxt):
                if s:
                    s.kill()


# ---------------------------------------------------------------------------
# Logos (the site's 32x32 pixel-art logomarks, same palette rules)
# ---------------------------------------------------------------------------

BASE_PALETTE = {"k": "#0b0f26", "n": "#2a3580", "s": "#8899cc", "p": "#e8eaf0", "g": "#ffd740", "o": "#d2500c"}


def _hex_to_hsv(c):
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    h = 0.0
    if d:
        if mx == r:
            h = ((g - b) / d + (6 if g < b else 0)) / 6
        elif mx == g:
            h = ((b - r) / d + 2) / 6
        else:
            h = ((r - g) / d + 4) / 6
    return h * 360, (d / mx * 100 if mx else 0), mx * 100


def _hsv_to_hex(h, s, v):
    s, v = s / 100, v / 100

    def ch(n):
        i = (n + h / 60) % 6
        return round(255 * (v - v * s * max(0, min(i, 4 - i, 1))))
    return "#%02x%02x%02x" % (ch(5), ch(3), ch(1))


def _nudge_hue(h, toward, step):
    r = (toward - h + 540) % 360 - 180
    return (h + (1 if r > 0 else -1 if r < 0 else 0) * min(step, abs(r)) + 360) % 360


def logo_palette(color):
    c = color.lower() if isinstance(color, str) and len(color) == 7 and color.startswith("#") else "#666666"
    h, s, v = _hex_to_hsv(c)
    pal = dict(BASE_PALETTE, c=c)
    pal["d"] = _hsv_to_hex(_nudge_hue(h, 255, 14), min(100, s * 1.1), v * 0.6)
    pal["l"] = _hsv_to_hex(_nudge_hue(h, 50, 10), s * 0.6, v + (100 - v) * 0.6)
    return pal


def render_logo(channel, scale=8, pad=2):
    lm = channel.get("logomark") or {}
    rows = lm.get("rows") if lm.get("type") == "pixel" else None
    pal = logo_palette(channel.get("color"))
    pal.update({k: v for k, v in (lm.get("palette") or {}).items()    # custom channels: extra colors
                if len(k) == 1 and re.match(r"^#[0-9a-fA-F]{6}$", str(v))})
    if not rows:
        rows = ["c" * 32] * 32
    n = len(rows)
    size = (n + pad * 2) * scale
    rgba = {k: bytes(int(v[i:i + 2], 16) for i in (1, 3, 5)) + b"\xff" for k, v in pal.items()}
    clear = b"\x00\x00\x00\x00"
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        ry = y // scale - pad
        for x in range(size):
            rx = x // scale - pad
            ch = rows[ry][rx] if 0 <= ry < n and 0 <= rx < len(rows[ry]) else "."
            raw += clear if ch == "." else rgba.get(ch, rgba["p"])

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# M3U / XMLTV / status page
# ---------------------------------------------------------------------------

def tvg_id(cid):
    return "spelunkr.%s" % cid


def build_m3u(catalog, base):
    lines = ['#EXTM3U url-tvg="%s/epg.xml" x-tvg-url="%s/epg.xml"' % (base, base)]
    for c in list(catalog.channels):
        cid = c["id"]
        lines.append('#EXTINF:-1 tvg-id="%s" tvg-chno="%s" tvg-name="%s" tvg-logo="%s/logo/%s.png" group-title="Spelunkr",%s'
                     % (tvg_id(cid), c.get("dial", ""), c["name"], base, cid, c["name"]))
        lines.append("%s/stream/%s.ts" % (base, cid))
    return "\n".join(lines) + "\n"


def _xmltv_time(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def _as_list(v):
    if not v:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [s.strip() for s in str(v).split(",") if s.strip()]


def build_xmltv(catalog, base, hours_back=2, hours_ahead=72):
    t = now_ms()
    t0, t1 = t - hours_back * 3600_000, t + hours_ahead * 3600_000
    e = lambda s: xml_escape(str(s))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="spelunkr2jellyfin" source-info-url="https://spelunkr.tv/">']
    chans = list(catalog.channels)
    for c in chans:
        out.append('  <channel id="%s"><display-name>%s</display-name><display-name>%s</display-name>'
                   '<icon src="%s/logo/%s.png"/><url>https://spelunkr.tv/</url></channel>'
                   % (tvg_id(c["id"]), e(c["name"]), e(c.get("dial", "")), base, c["id"]))
    for c in chans:
        cid, fam = c["id"], (c.get("family") or "")
        for p in catalog.programmes(cid, t0, t1):
            out.append('  <programme start="%s" stop="%s" channel="%s">'
                       % (_xmltv_time(p["start"]), _xmltv_time(p["stop"]), tvg_id(cid)))
            if p["item"] is None:
                out.append('    <title lang="en">Station Break</title>')
                out.append('  </programme>')
                continue
            ver = p["ver"]
            it, det = ver.items[p["item"]], ver.detail(p["item"])
            title = it.get("progTitle") or it.get("title") or it["id"]
            out.append('    <title lang="en">%s</title>' % e(title))
            if it.get("show") and it["show"] not in (c["name"], title):
                out.append('    <sub-title lang="en">%s</sub-title>' % e(it["show"]))
            desc = (det.get("description") or it.get("progDesc")
                    or (catalog.description(it["id"]) if it.get("youtube") else None) or c.get("description"))
            if desc:
                out.append('    <desc lang="en">%s</desc>' % e(desc))
            directors, stars = _as_list(det.get("director")), _as_list(det.get("stars"))
            if directors or stars:
                out.append("    <credits>%s%s</credits>" % (
                    "".join("<director>%s</director>" % e(d) for d in directors),
                    "".join("<actor>%s</actor>" % e(a) for a in stars)))
            year = str(it.get("year") or "")[:4]
            if year.isdigit():
                out.append("    <date>%s</date>" % year)
            cats = []
            if fam == "film":
                cats.append("Movie")
            if cid in KIDS_CHANNELS:
                cats.append("Kids")
            if cid in SPORTS_CHANNELS:
                cats.append("Sports")
            if fam:
                cats.append(fam.replace("-", " ").title())
            if it.get("show") and it["show"] != c["name"]:
                cats.append(it["show"])
            cats.append(c["name"].title())
            for cat in cats:
                out.append('    <category lang="en">%s</category>' % e(cat))
            if it.get("youtube"):
                out.append('    <icon src="https://i.ytimg.com/vi/%s/hqdefault.jpg"/>' % e(it["id"]))
                out.append("    <url>https://www.youtube.com/watch?v=%s</url>" % e(it["id"]))
            else:
                out.append('    <icon src="https://archive.org/services/img/%s"/>' % e(urllib.parse.quote(it["id"])))
                out.append("    <url>https://archive.org/details/%s</url>" % e(urllib.parse.quote(it["id"])))
            if det.get("season") or det.get("episode"):
                s, ep = det.get("season"), det.get("episode")
                out.append('    <episode-num system="onscreen">%s%s</episode-num>'
                           % ("S%s" % s if s else "", "E%s" % ep if ep else ""))
            if det.get("imdbId"):
                out.append('    <episode-num system="imdb.com">title/%s/</episode-num>' % e(det["imdbId"]))
            out.append("  </programme>")
    out.append("</tv>")
    return "\n".join(out) + "\n"


def now_playing(catalog, cid, t=None):
    t = now_ms() if t is None else t
    ver, e, start, end = catalog.entry_at(cid, t)
    if e["type"] == "video":
        title = ver.items[e["videoIndex"]].get("title")
    elif e["type"] == "ad":
        title = "Commercial break"
    elif e["type"] == "up-next":
        title = "Up Next card"
    else:
        title = "Bump break"
    return {"type": e["type"], "title": title, "start": start, "end": end, "version": ver.v,
            "videoIndex": e.get("videoIndex"), "slice": (e.get("slice") or {}).get("id"),
            "channelBump": (e.get("channelBump") or {}).get("id"), "platformBump": (e.get("platformBump") or {}).get("id")}


def status_page(catalog, base):
    t = now_ms()
    rows = []
    for c in list(catalog.channels):
        np = now_playing(catalog, c["id"], t)
        left = int((np["end"] - t) / 1000)
        rows.append("<tr><td>%s</td><td><img src='/logo/%s.png' width=32 height=32></td><td>%s</td><td>%s</td>"
                    "<td>%d:%02d left</td><td><a href='/stream/%s.ts'>stream</a></td></tr>"
                    % (c.get("dial", ""), c["id"], html.escape(c["name"]), html.escape(np["title"] or ""),
                       left // 60, left % 60, c["id"]))
    return ("<!doctype html><meta charset=utf-8><title>Spelunkr for Jellyfin</title>"
            "<style>body{font:15px system-ui;background:#0b0f26;color:#e8eaf0;margin:2em}a{color:#ffd740}"
            "td{padding:4px 10px;border-bottom:1px solid #2a3580}</style>"
            "<h1>Spelunkr &rarr; Jellyfin</h1><p>M3U: <a href='/playlist.m3u'>%s/playlist.m3u</a><br>"
            "XMLTV: <a href='/epg.xml'>%s/epg.xml</a></p><table>%s</table>"
            "<p>All programming courtesy of the <a href='https://archive.org/donate'>Internet Archive</a>. "
            "Schedule data from <a href='https://spelunkr.tv/'>spelunkr.tv</a>.</p>" % (base, base, "".join(rows)))


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "spelunkr2jellyfin/2.0"
    catalog = None
    streamer = None
    public_url = None
    hours_ahead = 72
    _logo_cache = {}

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def base(self):
        return self.public_url or "http://%s" % (self.headers.get("Host") or "localhost")

    def _send(self, code, body, ctype, head_only=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET(head_only=True)

    def do_GET(self, head_only=False):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                return self._send(200, status_page(self.catalog, self.base()), "text/html; charset=utf-8", head_only)
            if path == "/now.json":
                data = {c["id"]: now_playing(self.catalog, c["id"]) for c in self.catalog.channels}
                return self._send(200, json.dumps(data), "application/json", head_only)
            if path in ("/playlist.m3u", "/playlist.m3u8", "/channels.m3u"):
                return self._send(200, build_m3u(self.catalog, self.base()), "audio/x-mpegurl; charset=utf-8", head_only)
            if path in ("/epg.xml", "/xmltv.xml", "/guide.xml"):
                return self._send(200, build_xmltv(self.catalog, self.base(), hours_ahead=self.hours_ahead),
                                  "application/xml; charset=utf-8", head_only)
            if path.startswith("/logo/") and path.endswith(".png"):
                c = self.catalog.channel(path[6:-4])
                if not c:
                    return self._send(404, "no such channel", "text/plain")
                key = (c["id"], c.get("color"), json.dumps(c.get("logomark"), sort_keys=True))
                if key not in self._logo_cache:
                    self._logo_cache[key] = render_logo(c)
                return self._send(200, self._logo_cache[key], "image/png", head_only)
            if path.startswith("/stream/"):
                cid = path[8:].rsplit(".", 1)[0]
                if not self.catalog.channel(cid):
                    return self._send(404, "no such channel", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                if head_only:
                    return
                log.info("tune-in: %s from %s", cid, self.client_address[0])
                self.streamer.stream(cid, self.wfile)
                log.info("tune-out: %s from %s", cid, self.client_address[0])
                return
            return self._send(404, "not found", "text/plain", head_only)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def hw_args(hw):
    """ffmpeg options that open the GPU for encoding."""
    if not hw or not hw[1]:
        return []      # Windows encoders take normal frames and open the GPU themselves
    kind, dev = hw
    if kind == "qsv":
        return ["-init_hw_device", "vaapi=va:%s" % dev, "-init_hw_device", "qsv=hw@va", "-filter_hw_device", "hw"]
    return ["-vaapi_device", dev]


def hw_tail(hw):
    """End of every video filter chain: hand frames to the GPU when encoding there."""
    if not hw:
        return ""
    if not hw[1]:
        return ",format=nv12"
    return ",format=nv12,hwupload=extra_hw_frames=64" if hw[0] == "qsv" else ",format=nv12,hwupload"


HW_NAMES = {"qsv": "Intel Quick Sync", "vaapi": "VAAPI", "nvenc": "NVIDIA NVENC", "amf": "AMD AMF"}


def probe_hw(ffmpeg, mode, device):
    """Pick a GPU encoder that actually works, by doing a one-second test encode.
    Linux: auto tries Quick Sync, then VAAPI, on each /dev/dri/renderD* (or the given device).
    Windows: auto tries NVIDIA, then Intel Quick Sync, then AMD."""
    if mode == "none":
        return None
    if WINDOWS:
        kinds = ["nvenc", "qsv", "amf"] if mode == "auto" else [mode]
        devices = [None]
    else:
        kinds = ["qsv", "vaapi"] if mode == "auto" else [mode]
        devices = [device] if device != "auto" else sorted(
            os.path.join("/dev/dri", d) for d in (os.listdir("/dev/dri") if os.path.isdir("/dev/dri") else [])
            if d.startswith("renderD"))
    for dev in devices:
        if dev and not os.path.exists(dev):
            continue
        for kind in kinds:
            hw = (kind, dev)
            test = ([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"] + hw_args(hw)
                    + ["-f", "lavfi", "-i", "testsrc=size=640x480:rate=30", "-t", "1", "-vf", hw_tail(hw).lstrip(","),
                       "-c:v", "h264_%s" % kind, "-f", "null", "-"])
            try:
                out = subprocess.run(test, capture_output=True, text=True, timeout=30, **NO_WINDOW)
                if out.returncode == 0:
                    return hw
                log.info("GPU test: %s on %s didn't work (%s)", kind, dev,
                         " | ".join((out.stderr or "").strip().splitlines()[-1:]))
            except Exception as e:
                log.info("GPU test: %s on %s failed: %s", kind, dev, e)
    return None


def refresher(catalog):
    while True:
        time.sleep(REFRESH_SECONDS)
        try:
            catalog.refresh()
        except Exception as e:
            log.warning("lineup refresh failed, keeping the old one: %s", e)


def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser(description="Serve spelunkr.tv channels as M3U + XMLTV live TV for Jellyfin.")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--public-url", help="base URL Jellyfin uses to reach this server, e.g. http://192.168.1.20:8765 "
                                         "(default: taken from each request's Host header)")
    ap.add_argument("--size", default="640x480", help="output resolution, e.g. 640x480 or 854x480 (default 640x480)")
    ap.add_argument("--preset", default="veryfast", help="x264 preset (default veryfast)")
    ap.add_argument("--maxrate", type=int, default=2000, help="max video bitrate in kbps (default 2000)")
    ap.add_argument("--hours", type=int, default=72, help="hours of guide data to publish (default 72)")
    ap.add_argument("--lead", type=float, default=0.0,
                    help="run this many seconds ahead of spelunkr.tv, to cancel out your player's buffering delay "
                         "(e.g. 8 if Jellyfin shows things 8s after the website does)")
    ap.add_argument("--only", help="comma-separated channel ids to include")
    ap.add_argument("--skip", help="comma-separated channel ids to leave out")
    ap.add_argument("--include-hidden", action="store_true", help="also include 'coming soon' / debug channels")
    ap.add_argument("--font", help="font file for bump cards (default: star4000.ttf next to the script, else DejaVu/Arial; "
                                   "star4000-large.ttf is used for small print)")
    ap.add_argument("--cache", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"),
                    help="folder for saved schedule files (default: ./cache next to the script)")
    ap.add_argument("--channels", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.json"),
                    help="your own extra channels (default: channels.json next to the script)")
    ap.add_argument("--export", metavar="DIR", help="write playlist.m3u and epg.xml to DIR and exit (needs --public-url)")
    ap.add_argument("--now", action="store_true", help="print the schedule on every channel from now (or --at) and exit")
    ap.add_argument("--at", type=int, help="with --now: a time in epoch milliseconds")
    ap.add_argument("--dump", type=int, default=1, help="with --now: how many entries to print per channel")
    ap.add_argument("--hwaccel", choices=["auto", "qsv", "vaapi", "nvenc", "amf", "none"], default="auto",
                    help="encode on the GPU: Intel Quick Sync (qsv), VAAPI (Linux), NVIDIA (nvenc) or AMD (amf, "
                         "Windows). auto tries what fits this OS, then falls back to the CPU")
    ap.add_argument("--gpu-device", default="auto",
                    help="GPU render node, e.g. /dev/dri/renderD129 (auto: try each one in /dev/dri)")
    ap.add_argument("--no-logo-bug", action="store_true", help="don't show the channel logo in the corner during shows")
    ap.add_argument("--no-static", action="store_true", help="don't show TV snow while a channel tunes in")
    ap.add_argument("--no-vhs-cards", action="store_true", help="draw bump cards clean instead of as worn VHS")
    ap.add_argument("--ffmpeg", default=find_tool("ffmpeg"),
                    help="ffmpeg to use (default: on PATH, else Jellyfin's copy on Windows)")
    ap.add_argument("--ffprobe", default=find_tool("ffprobe"))
    ap.add_argument("--log-file", help="also write the log to this file (kept under ~10 MB)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    handlers = []
    if sys.stderr:                       # pythonw.exe has no console
        handlers.append(logging.StreamHandler())
    if a.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(a.log_file, maxBytes=5_000_000, backupCount=1,
                                                             encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, handlers=handlers or None,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        os.makedirs(a.cache, exist_ok=True)
        CACHE_DIR = a.cache
    except OSError as e:
        log.warning("can't use cache folder %s: %s", a.cache, e)
    split = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else None
    catalog = Catalog(a.include_hidden, split(a.only), split(a.skip), a.channels)
    catalog.fetch_descriptions = not (a.now or a.export)
    catalog.refresh()
    public = a.public_url.rstrip("/") if a.public_url else None

    if a.now:
        t = a.at or now_ms()
        out = {}
        for c in catalog.channels:
            rows, tt = [], t
            for _ in range(a.dump):
                np = now_playing(catalog, c["id"], tt)
                rows.append([np["type"], np["start"], np["end"], np["videoIndex"], np["slice"],
                             np["channelBump"], np["platformBump"]])
                tt = np["end"]
            out[c["id"]] = rows
        print(json.dumps({"t": t, "channels": out}))
        return
    if a.export:
        if not public:
            sys.exit("--export needs --public-url so the playlist knows where the streams live")
        os.makedirs(a.export, exist_ok=True)
        with open(os.path.join(a.export, "playlist.m3u"), "w", encoding="utf-8") as f:
            f.write(build_m3u(catalog, public))
        with open(os.path.join(a.export, "epg.xml"), "w", encoding="utf-8") as f:
            f.write(build_xmltv(catalog, public, hours_ahead=a.hours))
        print("wrote playlist.m3u and epg.xml to", a.export)
        return

    for tool in (a.ffmpeg, a.ffprobe):
        if not shutil.which(tool):
            sys.exit("can't find %s - install ffmpeg (or point --ffmpeg/--ffprobe at Jellyfin's copy)" % tool)
    retro = find_font([a.font] if a.font else [], RETRO_FONTS)
    fonts = (retro, find_font(RETRO_SMALL_FONTS) or retro, find_font(PLAIN_FONTS))
    if not any(fonts):
        log.warning("no font file found for bump cards - install DejaVu fonts or pass --font")
    log.info("card fonts: %s", ", ".join(os.path.basename(f) for f in dict.fromkeys(f for f in fonts if f)))
    w, h = (int(x) for x in a.size.lower().split("x"))
    hw = probe_hw(a.ffmpeg, a.hwaccel, a.gpu_device)
    if a.hwaccel not in ("auto", "none") and not hw:
        log.warning("GPU encoding was requested but isn't working - using the CPU instead")
    log.info("encoding on %s", ("the GPU: %s%s" % (HW_NAMES[hw[0]], " on " + hw[1] if hw[1] else ""))
             if hw else "the CPU (x264)")
    Handler.catalog = catalog
    Handler.streamer = Streamer(catalog, a.ffmpeg, a.ffprobe, w, h, a.preset, a.maxrate, fonts, a.lead,
                                hw=hw, bug=not a.no_logo_bug, static=not a.no_static,
                                vhs_cards=not a.no_vhs_cards)
    Handler.public_url = public
    Handler.hours_ahead = a.hours
    threading.Thread(target=refresher, args=(catalog,), daemon=True).start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    shown = public or "http://<this-machine>:%d" % a.port
    log.info("ready - M3U: %s/playlist.m3u   XMLTV: %s/epg.xml", shown, shown)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
