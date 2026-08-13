#!/usr/bin/env python3
"""
One-time headless auth setup for Spotistream.

System dependencies (not in requirements.txt, must be installed separately):
  - librespot: https://github.com/librespot-org/librespot
      cargo install librespot
      (or download a pre-built binary from GitHub releases)

Steps:
  1. Reads config from config.yml (or CONFIG env var path)
  2. Runs Spotipy OAuth flow (your app's client_id) — prints URL, user pastes
     redirect URL back. Saves refresh_token into config.yml; this is used for
     Web API playback control calls (play/pause/skip).
  3. Runs a second OAuth flow using librespot's own client_id (PKCE, no
     secret needed) — prints URL, user pastes redirect URL back. Spotify
     only authorizes librespot's own client_id to open a Connect/Spirc
     session; a token minted from your app's client_id gets a stored
     session that authenticates fine but is denied at Spirc init with
     "INVALID_CREDENTIALS". This step is what actually lets librespot show
     up as a controllable device.
  4. Runs librespot briefly with --access-token (from step 3) to populate
     its credential cache.
  5. Verifies cache was populated.

Usage:
  python3 auth_setup.py
"""

import base64
import datetime
import hashlib
import os
import secrets
import subprocess
import sys
import time
import urllib.parse

import requests
import spotipy
import yaml
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "user-modify-playback-state user-read-playback-state streaming"

# librespot's own OAuth client_id (from `librespot --enable-oauth`). Spotify
# authorizes this client_id to open Connect/Spirc sessions; a personal
# Developer Dashboard app's client_id is not, so it must be used to mint the
# token that bootstraps librespot's credential cache.
LIBRESPOT_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"
LIBRESPOT_REDIRECT_URI = "http://127.0.0.1:8899/login"
LIBRESPOT_SCOPES = (
    "app-remote-control playlist-modify playlist-modify-private "
    "playlist-modify-public playlist-read playlist-read-collaborative "
    "playlist-read-private streaming ugc-image-upload user-follow-modify "
    "user-follow-read user-library-modify user-library-read user-modify "
    "user-modify-playback-state user-modify-private user-personalized "
    "user-read-birthdate user-read-currently-playing user-read-email "
    "user-read-play-history user-read-playback-position "
    "user-read-playback-state user-read-private user-read-recently-played "
    "user-top-read"
)


def load_config() -> dict:
    path = os.environ.get("CONFIG", "config.yml")
    with open(path) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    path = os.environ.get("CONFIG", "config.yml")
    with open(path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def run_oauth(cfg: dict) -> tuple[str, str]:
    """Run Spotipy OAuth flow, return (access_token, refresh_token)."""
    sp_cfg = cfg["spotify"]
    auth_manager = SpotifyOAuth(
        client_id=sp_cfg["client_id"],
        client_secret=sp_cfg["client_secret"],
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=SCOPES,
        open_browser=False,
        cache_handler=spotipy.cache_handler.MemoryCacheHandler(),
    )

    auth_url = auth_manager.get_authorize_url()
    print("\n=== Spotify Authorization ===")
    print("Open this URL in a browser (on your laptop/phone):")
    print()
    print(auth_url)
    print()
    print("After authorizing, you will be redirected to a URL starting with")
    print("http://127.0.0.1:8888/callback?code=...")
    print("(The page will fail to load — that's fine.)")
    print()
    redirect_response = input("Paste the full redirect URL here: ").strip()

    code = auth_manager.parse_response_code(redirect_response)
    token_info = auth_manager.get_access_token(code, check_cache=False)

    return token_info["access_token"], token_info["refresh_token"]


def run_librespot_oauth() -> str:
    """PKCE OAuth flow using librespot's own client_id, so the resulting
    access token is authorized to open a Spotify Connect session. Returns
    an access_token suitable for `librespot --access-token`."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": LIBRESPOT_CLIENT_ID,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": LIBRESPOT_REDIRECT_URI,
        "scope": LIBRESPOT_SCOPES,
    }
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)

    print("\n=== Librespot Connect Authorization ===")
    print("This second sign-in uses librespot's own client ID, which Spotify")
    print("authorizes to open a Connect/playback session (your app's client")
    print("ID above is not, and won't let the device connect).")
    print("Open this URL in a browser (on your laptop/phone):")
    print()
    print(url)
    print()
    print("After authorizing, you will be redirected to a URL starting with")
    print(f"{LIBRESPOT_REDIRECT_URI}?code=...")
    print("(The page will fail to load — that's fine.)")
    print()
    redirect_response = input("Paste the full redirect URL here: ").strip()

    qs = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_response).query)
    if "code" not in qs:
        print(f"ERROR: no 'code' param found in that URL: {redirect_response}")
        sys.exit(1)
    if qs.get("state", [""])[0] != state:
        print("ERROR: state mismatch — paste the URL from the link printed above.")
        sys.exit(1)
    code = qs["code"][0]

    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        data={
            "client_id": LIBRESPOT_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": LIBRESPOT_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def populate_librespot_cache(cfg: dict, access_token: str) -> None:
    """Run librespot briefly with --access-token to populate credential cache."""
    lb_cfg = cfg["librespot"]
    cache_dir = lb_cfg["cache_dir"]
    device_name = lb_cfg.get("device_name", "Spotistream")

    os.makedirs(cache_dir, exist_ok=True)

    librespot_bin = lb_cfg.get("path", "") or "librespot"
    cmd = [
        librespot_bin,
        "--name", device_name,
        "--access-token", access_token,
        "--cache", cache_dir,
        "--backend", "pipe",
        "--quiet",
    ]

    username = lb_cfg.get("username", "")
    if username:
        cmd += ["--username", username]

    print("\nStarting librespot to populate credential cache...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(5)
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Verify cache was populated
    if os.path.isdir(cache_dir) and os.listdir(cache_dir):
        print(f"Cache populated at: {cache_dir}")
    else:
        print(f"WARNING: Cache directory appears empty: {cache_dir}")
        print("librespot may have failed. Check that librespot is installed and the")
        print("access token was valid.")


def main() -> None:
    try:
        cfg = load_config()
    except FileNotFoundError:
        print("ERROR: config.yml not found. Copy config.example.yml to config.yml and fill in your credentials.")
        sys.exit(1)

    sp_cfg = cfg["spotify"]
    for key in ("client_id", "client_secret", "playlist_id"):
        if not sp_cfg.get(key) or sp_cfg[key].startswith("YOUR_"):
            print(f"ERROR: config.yml missing or placeholder value for spotify.{key}")
            sys.exit(1)

    _, refresh_token = run_oauth(cfg)

    cfg["spotify"]["refresh_token"] = refresh_token
    cfg["spotify"]["refresh_token_created_at"] = datetime.date.today().isoformat()
    cfg["spotify"]["refresh_token_warning_sent"] = False
    save_config(cfg)
    print(f"\nrefresh_token saved to config.yml (expires in ~6 months)")

    librespot_access_token = run_librespot_oauth()
    populate_librespot_cache(cfg, librespot_access_token)

    print("\nAuth setup complete!")
    print("You can now run: python3 stream.py")


if __name__ == "__main__":
    main()
