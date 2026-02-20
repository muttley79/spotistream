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
import logging
import logging.handlers
import os
import random
import subprocess
import sys
import threading
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

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

client_queues: list[asyncio.Queue] = []
client_count: int = 0
client_lock: asyncio.Lock  # initialised in on_startup (needs running loop)

watchdog_task: asyncio.Task | None = None

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

def broadcaster(loop: asyncio.AbstractEventLoop) -> None:
    """
    Read MP3 chunks from ffmpeg stdout and fan-out to all client queues.
    Blocks naturally when librespot is paused (no PCM data flowing).
    Runs as a daemon thread.
    """
    assert ffmpeg_proc.stdout is not None
    log.info("Broadcaster started")
    while True:
        try:
            chunk = ffmpeg_proc.stdout.read(8192)
        except Exception as exc:
            log.error("Broadcaster read error: %s", exc)
            break
        if not chunk:
            log.info("Broadcaster: ffmpeg stdout EOF — sending sentinel to all queues")
            snapshot = list(client_queues)
            for q in snapshot:
                loop.call_soon_threadsafe(_safe_sentinel, q)
            break
        snapshot = list(client_queues)
        for q in snapshot:
            loop.call_soon_threadsafe(_safe_put, q, chunk)
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


async def discover_device(timeout: float = 30.0) -> str:
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
            sys.exit(1)
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
                else:
                    log.debug("Watchdog: not playing (streak=%d, waiting to confirm)", not_playing_streak)
            else:
                not_playing_streak = 0
            backoff = 5.0
        except spotipy.exceptions.SpotifyException as exc:
            if exc.http_status == 429:
                retry_after = int(exc.headers.get("Retry-After", 10)) if exc.headers else 10
                wait = min(retry_after, 60)
                log.warning("Watchdog: 429 rate-limited, backing off %ds", wait)
                backoff = wait
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
    global client_count, watchdog_task

    # Register client
    async with client_lock:
        client_count += 1
        first_client = (client_count == 1)
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        client_queues.append(queue)
        log.info("Client connected (total=%d, addr=%s)", client_count, request.remote)

    if first_client:
        offset = random.randint(0, max(0, playlist_total - 1))
        try:
            result = await sp_call(
                sp.playlist_tracks,
                PLAYLIST_ID,
                offset=offset,
                limit=1,
                fields="items(track(duration_ms))",
            )
            duration_ms = result["items"][0]["track"]["duration_ms"]
            position_ms = random.randint(0, duration_ms // 2)
            await start_playlist_at_offset(offset, position_ms=position_ms)
        except Exception as exc:
            log.error("Failed to start playback: %s", exc)
        watchdog_task = asyncio.create_task(playback_watchdog())

    response = web.StreamResponse(headers={
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache",
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
                log.warning("Client %s timed out (no data for 30s)", request.remote)
                break
            if chunk is None:
                log.info("Client %s received EOF sentinel", request.remote)
                break
            try:
                await response.write(chunk)
            except Exception:
                break
    finally:
        async with client_lock:
            try:
                client_queues.remove(queue)
            except ValueError:
                pass
            client_count -= 1
            last_client = (client_count == 0)
            log.info("Client disconnected (total=%d, addr=%s)", client_count, request.remote)

        if last_client:
            if watchdog_task is not None:
                watchdog_task.cancel()
                watchdog_task = None
            async with client_lock:
                if client_count == 0:
                    await sp_pause()

    return response


async def handle_health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    global sp, librespot_device_id, playlist_total
    global librespot_proc, ffmpeg_proc, client_lock

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
        target=broadcaster, args=(loop,), daemon=True
    ).start()

    # Build spotipy client
    sp = build_spotipy_client()

    # Discover librespot device
    librespot_device_id = await discover_device(timeout=30.0)

    # Fetch playlist track count
    result = await sp_call(
        sp.playlist_tracks,
        PLAYLIST_ID,
        fields="total",
        limit=1,
    )
    playlist_total = result["total"]
    log.info("Playlist '%s' has %d tracks", PLAYLIST_ID, playlist_total)

    log.info("Spotistream ready on port %d — waiting for first client", PORT)


async def on_cleanup(app: web.Application) -> None:
    log.info("Shutting down...")
    if watchdog_task is not None:
        watchdog_task.cancel()
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
