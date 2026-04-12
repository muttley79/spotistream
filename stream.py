#!/usr/bin/env python3
"""
Spotistream — Spotify playlist HTTP audio streaming server.

A hardcoded playlist streams as a radio-style HTTP stream openable in VLC.
Multiple listeners share the same stream. Playback only runs on Spotify
when at least 1 client is connected.

System dependencies (not in requirements.txt, must be installed separately):
  - librespot: https://github.com/librespot-org/librespot
      cargo install librespot
      (or download a pre-built binary from GitHub releases)
  - ffmpeg:
      sudo apt install ffmpeg

Python dependencies:
  pip install -r requirements.txt

Setup (one time):
  1. Copy config.example.yml to config.yml and fill in client_id, client_secret, playlist_id
  2. python3 auth_setup.py   # OAuth flow + librespot credential cache bootstrap

Usage:
  python3 stream.py
  CONFIG=/path/to/config.yml python3 stream.py
"""

import asyncio
import base64
import collections
import glob
import itertools
import logging
import logging.handlers
import os
import random
import subprocess
import sys
import threading
import time
from functools import partial

import spotipy
import yaml
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    path = os.environ.get("CONFIG", "config.yml")
    with open(path) as f:
        return yaml.safe_load(f)


CFG = load_config()
SP_CFG = CFG["spotify"]
LB_CFG = CFG["librespot"]
FF_CFG = CFG.get("ffmpeg", {})
SRV_CFG = CFG.get("server", {})

PLAYLIST_ID: str = SP_CFG["playlist_id"]
DEVICE_NAME: str = LB_CFG.get("device_name", "Spotistream")
CACHE_DIR: str = LB_CFG["cache_dir"]
BITRATE: int = int(LB_CFG.get("bitrate", 320))
PORT: int = int(SRV_CFG.get("port", 8000))
LIBRESPOT_BIN: str    = LB_CFG.get("path", "") or "librespot"
FFMPEG_BIN: str       = FF_CFG.get("path", "") or "ffmpeg"
INITIAL_VOLUME: int   = int(LB_CFG.get("initial_volume", 100))
SCOPES = "user-modify-playback-state user-read-playback-state streaming"
BUFFER_SECS: float = float(SRV_CFG.get("buffer_secs", 10.0))
PROMOS_DIR: str = SRV_CFG.get("promos_dir", "")
AUTH_USER: str = SRV_CFG.get("auth_user", "")
AUTH_PASS: str = SRV_CFG.get("auth_password", "")
AUTH_ENABLED: bool = bool(AUTH_USER and AUTH_PASS)

if AUTH_ENABLED:
    log.info("Stream auth enabled (user='%s')", AUTH_USER)
else:
    log.info("Stream auth disabled (no auth_user/auth_password set)")


def _check_auth(request) -> bool:
    """Return True if the request carries valid Basic Auth credentials (or auth is disabled)."""
    if not AUTH_ENABLED:
        return True
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        user, _, password = decoded.partition(":")
        return user == AUTH_USER and password == AUTH_PASS
    except Exception:
        return False


def _unauthorized() -> web.Response:
    return web.Response(
        status=401,
        headers={"WWW-Authenticate": 'Basic realm="Spotistream"'},
        text="Unauthorized",
    )


def _add_file_handler() -> None:
    log_cfg = CFG.get("logging", {})
    log_file = log_cfg.get("log_file", "spotistream.log")
    max_bytes = int(log_cfg.get("max_bytes", 10 * 1024 * 1024))  # 10 MB default
    backup_count = int(log_cfg.get("backup_count", 3))
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(fh)
    log.info("File logging enabled: %s (max %dMB x %d)", log_file, max_bytes // 1024 // 1024, backup_count)

_add_file_handler()


def load_promo_files() -> list[str]:
    if not PROMOS_DIR:
        return []
    if not os.path.isdir(PROMOS_DIR):
        log.warning("Promos: directory '%s' not found — promos disabled", PROMOS_DIR)
        return []
    files = sorted(glob.glob(os.path.join(PROMOS_DIR, "*.mp3")))
    log.info("Promos: found %d .mp3 file(s) in '%s'", len(files), PROMOS_DIR)
    return files

PROMO_FILES: list[str] = load_promo_files()

# Must match the -b:a value in build_ffmpeg_cmd()
_OUTPUT_BITRATE_KBPS: int = 192
_OUTPUT_BYTES_PER_SEC: int = _OUTPUT_BITRATE_KBPS * 1000 // 8  # 24 000

# broadcaster reads 8192-byte chunks → ~3 chunks/s at 192 kbps
_CHUNK = 8192
_CHUNKS_IN_BUFFER = max(1, int(BUFFER_SECS * _OUTPUT_BYTES_PER_SEC / _CHUNK))
log.info("Ring buffer: BUFFER_SECS=%.1f  maxlen=%d chunks (~%.1f KB)  rate=%d kbps",
         BUFFER_SECS, _CHUNKS_IN_BUFFER, _CHUNKS_IN_BUFFER * _CHUNK / 1024,
         _OUTPUT_BITRATE_KBPS)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

client_queues: list[asyncio.Queue] = []
broadcaster_buffer: collections.deque[bytes] = collections.deque(maxlen=_CHUNKS_IN_BUFFER)
_conn_id_counter = itertools.count(1)   # monotonic per-connection ID for log correlation
client_count: int = 0
client_lock: asyncio.Lock  # initialised in on_startup (needs running loop)

watchdog_task: asyncio.Task | None = None
process_monitor_task: asyncio.Task | None = None

sp: spotipy.Spotify  # initialised in on_startup
librespot_device_id: str  # discovered in on_startup
playlist_total: int  # fetched in on_startup

librespot_proc: subprocess.Popen
ffmpeg_proc: subprocess.Popen

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def build_librespot_cmd() -> list[str]:
    cmd = [
        LIBRESPOT_BIN,
        "--name", DEVICE_NAME,
        "--cache", CACHE_DIR,
        "--backend", "pipe",
        "--bitrate", str(BITRATE),
        "--quiet",
        "--disable-audio-cache",
        "--initial-volume", str(INITIAL_VOLUME),
    ]
    username = LB_CFG.get("username", "")
    if username:
        cmd += ["--username", username]
    return cmd


def build_ffmpeg_cmd() -> list[str]:
    return [
        FFMPEG_BIN,
        "-f", "s16le",
        "-ar", "44100",
        "-ac", "2",
        "-i", "pipe:0",
        "-f", "mp3",
        "-b:a", "192k",
        "-reservoir", "0",
        "-fflags", "nobuffer",
        "-loglevel", "error",
        "pipe:1",
    ]


def drain_stderr(proc: subprocess.Popen, name: str) -> None:
    """Daemon thread: drain subprocess stderr to avoid pipe buffer deadlocks."""
    assert proc.stderr is not None
    for line in proc.stderr:
        text = line.rstrip(b"\n").decode(errors="replace")
        if text:
            log.debug("[%s stderr] %s", name, text)
    log.debug("[%s stderr] EOF", name)


# ---------------------------------------------------------------------------
# Broadcaster thread
# ---------------------------------------------------------------------------

def broadcaster(loop: asyncio.AbstractEventLoop, ffmpeg_stdout) -> None:
    """
    Read MP3 chunks from ffmpeg stdout and fan-out to all client queues.
    Blocks naturally when librespot is paused (no PCM data flowing).
    Runs as a daemon thread.
    """
    assert ffmpeg_stdout is not None
    log.info("Broadcaster started")
    while True:
        read_start = time.monotonic()
        try:
            chunk = ffmpeg_stdout.read(8192)
        except Exception as exc:
            log.error("Broadcaster read error: %s", exc)
            break
        if not chunk:
            log.info("Broadcaster: ffmpeg stdout EOF — sending sentinel to all queues")
            log.info("Broadcaster: clearing ring buffer (%d chunks)", len(broadcaster_buffer))
            broadcaster_buffer.clear()
            snapshot = list(client_queues)
            for q in snapshot:
                loop.call_soon_threadsafe(_safe_sentinel, q)
            break
        broadcaster_buffer.append(chunk)
        buf_len = len(broadcaster_buffer)
        if buf_len % 30 == 0:   # log roughly every ~10 s
            log.debug("Broadcaster: buffer fill %d/%d chunks, %d client(s)",
                      buf_len, _CHUNKS_IN_BUFFER, len(client_queues))
        snapshot = list(client_queues)
        for q in snapshot:
            loop.call_soon_threadsafe(_safe_put, q, chunk)

        # Rate-limit: each chunk represents (len/rate) seconds of audio.
        # If we got here faster (fast ffmpeg), sleep the remainder.
        # If ffmpeg was slow / blocked (paused), elapsed > chunk_secs → no sleep.
        chunk_secs = len(chunk) / _OUTPUT_BYTES_PER_SEC
        elapsed = time.monotonic() - read_start
        if chunk_secs > elapsed:
            time.sleep(chunk_secs - elapsed)
    log.info("Broadcaster exited")


def _safe_put(q: asyncio.Queue, chunk: bytes) -> None:
    try:
        q.put_nowait(chunk)
    except asyncio.QueueFull:
        log.debug("Queue full for a client — dropping chunk")


def _safe_sentinel(q: asyncio.Queue) -> None:
    try:
        q.put_nowait(None)
    except asyncio.QueueFull:
        pass


def inject_promo(loop: asyncio.AbstractEventLoop, path: str) -> None:
    """
    Read a promo MP3 file and inject it into the live stream.
    Runs in a thread executor while Spotify is paused (broadcaster is blocked).
    Rate-limited identically to the broadcaster: one chunk_secs sleep per chunk.
    Promo files should be 192 kbps MP3 to match _OUTPUT_BYTES_PER_SEC.
    """
    log.info("Promo: injecting '%s'", path)
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                broadcaster_buffer.append(chunk)
                snapshot = list(client_queues)
                for q in snapshot:
                    loop.call_soon_threadsafe(_safe_put, q, chunk)
                chunk_secs = len(chunk) / _OUTPUT_BYTES_PER_SEC
                time.sleep(chunk_secs)
    except Exception as exc:
        log.error("Promo: error injecting '%s': %s", path, exc)
    log.info("Promo: done")


# ---------------------------------------------------------------------------
# Spotipy helpers
# ---------------------------------------------------------------------------

def build_spotipy_client() -> spotipy.Spotify:
    refresh_token = SP_CFG.get("refresh_token", "")
    if not refresh_token:
        log.error("No refresh_token in config.yml. Run auth_setup.py first.")
        sys.exit(1)

    cache_handler = spotipy.cache_handler.MemoryCacheHandler(token_info={
        "refresh_token": refresh_token,
        "expires_at": 0,       # forces immediate refresh on first API call
        "access_token": "",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": SCOPES,
    })
    return spotipy.Spotify(
        auth_manager=spotipy.oauth2.SpotifyOAuth(
            client_id=SP_CFG["client_id"],
            client_secret=SP_CFG["client_secret"],
            redirect_uri="http://127.0.0.1:8888/callback",
            scope=SCOPES,
            cache_handler=cache_handler,
            open_browser=False,
        )
    )


async def sp_call(fn, *args, **kwargs):
    """Run a blocking spotipy call in the default executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(fn, *args, **kwargs))


async def discover_device(timeout: float = 30.0, exit_on_timeout: bool = True) -> str:
    """Poll sp.devices() until the librespot device appears."""
    log.info("Waiting for librespot device '%s' to appear (up to %.0fs)...", DEVICE_NAME, timeout)
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            devices = await sp_call(sp.devices)
            for d in devices.get("devices", []):
                if d["name"] == DEVICE_NAME:
                    log.info("Found device: %s (id=%s)", d["name"], d["id"])
                    return d["id"]
        except Exception as exc:
            log.warning("devices() error: %s", exc)
        if asyncio.get_event_loop().time() >= deadline:
            log.error("Timed out waiting for librespot device. Is librespot running?")
            if exit_on_timeout:
                sys.exit(1)
            raise RuntimeError("Timed out waiting for librespot device")
        await asyncio.sleep(2)


async def start_playlist_at_offset(offset: int, position_ms: int = 0) -> None:
    log.info("Starting playlist at offset %d, position %dms (device=%s)", offset, position_ms, librespot_device_id)
    await sp_call(
        sp.start_playback,
        device_id=librespot_device_id,
        context_uri=f"spotify:playlist:{PLAYLIST_ID}",
        offset={"position": offset},
        position_ms=position_ms,
    )


async def sp_pause() -> None:
    try:
        await sp_call(sp.pause_playback, device_id=librespot_device_id)
        log.info("Playback paused")
    except Exception as exc:
        log.warning("pause_playback error (may already be paused): %s", exc)


# ---------------------------------------------------------------------------
# Playback watchdog (end-of-playlist looping)
# ---------------------------------------------------------------------------

async def playback_watchdog() -> None:
    """
    Asyncio task, runs only while clients are connected.
    Polls current_playback(); if not playing for 2 consecutive polls, restarts
    playlist from track 0 (handles end-of-playlist). The debounce avoids false
    positives during the brief gap between tracks.
    """
    log.info("Watchdog started")
    backoff = 5.0
    not_playing_streak = 0
    last_track_uri: str | None = None
    songs_played: int = 0
    next_promo_at: int = random.randint(3, 5)
    while True:
        await asyncio.sleep(backoff)
        try:
            playback = await sp_call(sp.current_playback)
            if playback is None or not playback.get("is_playing"):
                not_playing_streak += 1
                if not_playing_streak >= 2:
                    log.info("Watchdog: playback stopped (confirmed) — restarting from track 0")
                    await start_playlist_at_offset(0)
                    not_playing_streak = 0
                    last_track_uri = None   # reset so next song isn't double-counted
                else:
                    log.debug("Watchdog: not playing (streak=%d, waiting to confirm)", not_playing_streak)
            else:
                not_playing_streak = 0
                if PROMO_FILES and client_count > 0:
                    current_uri = (playback.get("item") or {}).get("uri")
                    if current_uri and current_uri != last_track_uri:
                        if last_track_uri is not None:   # skip counter on very first song
                            songs_played += 1
                            log.debug("Watchdog: song transition (%d played, promo at %d)",
                                      songs_played, next_promo_at)
                            if songs_played >= next_promo_at:
                                songs_played = 0
                                next_promo_at = random.randint(3, 5)
                                promo_path = random.choice(PROMO_FILES)
                                log.info("Watchdog: promo time — pausing, injecting '%s'",
                                         promo_path)
                                await sp_pause()
                                cur_loop = asyncio.get_running_loop()
                                await cur_loop.run_in_executor(
                                    None, inject_promo, cur_loop, promo_path
                                )
                                try:
                                    await sp_call(sp.start_playback,
                                                  device_id=librespot_device_id)
                                    log.info("Watchdog: promo done, Spotify resumed")
                                except Exception as exc:
                                    log.warning("Watchdog: resume after promo failed: %s", exc)
                        last_track_uri = current_uri
            backoff = 5.0
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status == 429:
                retry_after = int(exc.headers.get("Retry-After", 10)) if exc.headers else 10
                wait = min(retry_after, 60)
                log.warning("Watchdog: 429 rate-limited, backing off %ds", wait)
                backoff = wait
            elif exc.http_status == 404 and "Device not found" in str(exc):
                log.warning("Watchdog: device not found (404) — restarting pipeline")
                await restart_pipeline(asyncio.get_running_loop())
                backoff = 5.0
            else:
                log.warning("Watchdog spotipy error: %s", exc)
                backoff = min(backoff * 2, 60)
        except asyncio.CancelledError:
            log.info("Watchdog cancelled")
            return
        except Exception as exc:
            log.warning("Watchdog error: %s", exc)
            backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

async def handle_stream(request: web.Request) -> web.StreamResponse:
    if not _check_auth(request):
        log.warning("Auth failure from %s", request.remote)
        return _unauthorized()

    global client_count, watchdog_task

    conn_id = next(_conn_id_counter)
    ua = request.headers.get("User-Agent", "-")
    range_hdr = request.headers.get("Range", "-")
    is_probe = "Range" in request.headers
    log.info("[conn=%d] Incoming %s /stream  addr=%s  probe=%s  Range=%s  User-Agent=%s",
             conn_id, request.method, request.remote, is_probe, range_hdr, ua)

    if is_probe:
        # VLC (and some other players) sends a Range: bytes=0- probe first to check
        # whether the server supports seeking.  We don't — respond with Accept-Ranges: none
        # and stream without registering as a real client so the probe doesn't affect
        # client_count, first_client logic, or the ring-buffer pre-fill.
        log.info("[conn=%d] probe (Range present) — serving without registration", conn_id)
        response = web.StreamResponse(headers={
            "Content-Type": "audio/mpeg",
            "Cache-Control": "no-cache",
            "Accept-Ranges": "none",
            "X-Content-Type-Options": "nosniff",
            "icy-name": "Spotistream",
            "icy-genre": "Music",
        })
        if request.version >= (1, 1):
            response.enable_chunked_encoding()
        await response.prepare(request)
        # VLC closes this connection immediately after reading headers; just drain quietly.
        probe_q: asyncio.Queue = asyncio.Queue(maxsize=8)
        async with client_lock:
            client_queues.append(probe_q)
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(probe_q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    break
                if chunk is None:
                    break
                try:
                    await response.write(chunk)
                except Exception:
                    break
        finally:
            async with client_lock:
                try:
                    client_queues.remove(probe_q)
                except ValueError:
                    pass
            log.info("[conn=%d] probe disconnected", conn_id)
        return response

    # Register real client
    async with client_lock:
        client_count += 1
        first_client = (client_count == 1)
        queue: asyncio.Queue = asyncio.Queue(maxsize=400)
        buf_snapshot = list(broadcaster_buffer)
        prefilled = 0
        for chunk in buf_snapshot:
            try:
                queue.put_nowait(chunk)
                prefilled += 1
            except asyncio.QueueFull:
                log.warning("[conn=%d] queue full during pre-fill after %d/%d chunks",
                            conn_id, prefilled, len(buf_snapshot))
                break
        client_queues.append(queue)
        log.info("[conn=%d] registered (total=%d, first=%s) — pre-filled %d/%d buffer chunks (%.1f KB)",
                 conn_id, client_count, first_client, prefilled, len(buf_snapshot),
                 prefilled * _CHUNK / 1024)

    if first_client:
        offset = random.randint(0, max(0, playlist_total - 1))
        try:
            result = await sp_call(
                sp._get,
                f"playlists/{PLAYLIST_ID}/items",
                offset=offset,
                limit=1,
                fields="items(item(duration_ms))",
            )
            duration_ms = result["items"][0]["item"]["duration_ms"]
            position_ms = random.randint(0, duration_ms // 2)
            await start_playlist_at_offset(offset, position_ms=position_ms)
        except Exception as exc:
            log.error("Failed to start playback: %s", exc)
        watchdog_task = asyncio.create_task(playback_watchdog())

    response = web.StreamResponse(headers={
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache",
        "Accept-Ranges": "none",
        "X-Content-Type-Options": "nosniff",
        "icy-name": "Spotistream",
        "icy-genre": "Music",
    })
    if request.version >= (1, 1):
        response.enable_chunked_encoding()
    await response.prepare(request)

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                log.warning("[conn=%d] timed out (no data for 30s, addr=%s)", conn_id, request.remote)
                break
            if chunk is None:
                log.info("[conn=%d] received EOF sentinel", conn_id)
                break
            try:
                await response.write(chunk)
            except Exception as exc:
                log.info("[conn=%d] write error: %s", conn_id, exc)
                break
    finally:
        async with client_lock:
            try:
                client_queues.remove(queue)
            except ValueError:
                pass
            client_count -= 1
            last_client = (client_count == 0)
            log.info("[conn=%d] disconnected (total=%d, last=%s, addr=%s)",
                     conn_id, client_count, last_client, request.remote)

        if last_client:
            if watchdog_task is not None:
                watchdog_task.cancel()
                watchdog_task = None
            async with client_lock:
                if client_count == 0:
                    await sp_pause()
                    broadcaster_buffer.clear()
                    log.info("Ring buffer cleared on last-client disconnect")

    return response


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def restart_pipeline(loop) -> None:
    global librespot_proc, ffmpeg_proc, librespot_device_id
    log.info("restart_pipeline: tearing down old processes")
    for proc, name in [(ffmpeg_proc, "ffmpeg"), (librespot_proc, "librespot")]:
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception as exc:
            log.warning("restart_pipeline: error stopping %s: %s", name, exc)
    lb_cmd = build_librespot_cmd()
    log.info("restart_pipeline: starting librespot: %s", " ".join(lb_cmd))
    librespot_proc = subprocess.Popen(lb_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ff_cmd = build_ffmpeg_cmd()
    log.info("restart_pipeline: starting ffmpeg: %s", " ".join(ff_cmd))
    ffmpeg_proc = subprocess.Popen(ff_cmd, stdin=librespot_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    librespot_proc.stdout.close()
    threading.Thread(target=drain_stderr, args=(librespot_proc, "librespot"), daemon=True).start()
    threading.Thread(target=drain_stderr, args=(ffmpeg_proc, "ffmpeg"), daemon=True).start()
    threading.Thread(target=broadcaster, args=(loop, ffmpeg_proc.stdout), daemon=True).start()
    try:
        librespot_device_id = await discover_device(timeout=30.0, exit_on_timeout=False)
    except RuntimeError as exc:
        log.error("restart_pipeline: device discovery failed: %s", exc)
        return
    if client_count > 0:
        try:
            await sp_call(sp.start_playback, device_id=librespot_device_id)
        except Exception as exc:
            log.warning("restart_pipeline: resume playback failed: %s", exc)
    log.info("restart_pipeline: done — new device_id=%s", librespot_device_id)


async def process_monitor(loop) -> None:
    log.info("Process monitor started")
    while True:
        try:
            await asyncio.sleep(5)
            if librespot_proc.poll() is not None:
                log.warning("Process monitor: librespot exited (code=%s) — restarting pipeline",
                            librespot_proc.returncode)
                await restart_pipeline(loop)
            elif ffmpeg_proc.poll() is not None:
                log.warning("Process monitor: ffmpeg exited (code=%s) — restarting pipeline",
                            ffmpeg_proc.returncode)
                await restart_pipeline(loop)
        except asyncio.CancelledError:
            log.info("Process monitor cancelled")
            return
        except Exception as exc:
            log.error("Process monitor: unexpected error: %s", exc)


async def on_startup(app: web.Application) -> None:
    global sp, librespot_device_id, playlist_total
    global librespot_proc, ffmpeg_proc, client_lock, process_monitor_task

    client_lock = asyncio.Lock()
    loop = asyncio.get_running_loop()

    # Start librespot
    lb_cmd = build_librespot_cmd()
    log.info("Starting librespot: %s", " ".join(lb_cmd))
    librespot_proc = subprocess.Popen(
        lb_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Start ffmpeg (reads from librespot stdout)
    ff_cmd = build_ffmpeg_cmd()
    log.info("Starting ffmpeg: %s", " ".join(ff_cmd))
    ffmpeg_proc = subprocess.Popen(
        ff_cmd,
        stdin=librespot_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Close parent's reference to librespot stdout (ffmpeg owns it now)
    # This prevents ffmpeg from hanging on EOF if librespot dies
    assert librespot_proc.stdout is not None
    librespot_proc.stdout.close()

    # Drain stderr for both processes
    threading.Thread(
        target=drain_stderr, args=(librespot_proc, "librespot"), daemon=True
    ).start()
    threading.Thread(
        target=drain_stderr, args=(ffmpeg_proc, "ffmpeg"), daemon=True
    ).start()

    # Start broadcaster
    threading.Thread(
        target=broadcaster, args=(loop, ffmpeg_proc.stdout), daemon=True
    ).start()

    # Build spotipy client
    sp = build_spotipy_client()

    # Discover librespot device
    librespot_device_id = await discover_device(timeout=30.0)

    # Fetch playlist track count
    result = await sp_call(
        sp._get,
        f"playlists/{PLAYLIST_ID}/items",
        fields="total",
        limit=1,
    )
    playlist_total = result["total"]
    log.info("Playlist '%s' has %d tracks", PLAYLIST_ID, playlist_total)

    process_monitor_task = asyncio.create_task(process_monitor(loop))

    log.info("Spotistream ready on port %d — waiting for first client", PORT)


async def on_cleanup(app: web.Application) -> None:
    log.info("Shutting down...")
    if watchdog_task is not None:
        watchdog_task.cancel()
    if process_monitor_task is not None:
        process_monitor_task.cancel()
    for proc, name in [(ffmpeg_proc, "ffmpeg"), (librespot_proc, "librespot")]:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception as exc:
            log.warning("Error stopping %s: %s", name, exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = web.Application()
    app.router.add_get("/stream", handle_stream)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, port=PORT, access_log=None)


if __name__ == "__main__":
    main()
