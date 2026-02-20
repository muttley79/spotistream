#!/usr/bin/env python3
"""
One-time headless auth setup for Spotistream.

System dependencies (not in requirements.txt, must be installed separately):
  - librespot: https://github.com/librespot-org/librespot
      cargo install librespot
      (or download a pre-built binary from GitHub releases)

Steps:
  1. Reads config from config.yml (or CONFIG env var path)
  2. Runs Spotipy OAuth flow — prints URL, user pastes redirect URL back
  3. Saves refresh_token into config.yml
  4. Runs librespot briefly with --access-token to populate credential cache
  5. Verifies cache was populated

Usage:
  python3 auth_setup.py
"""

import os
import subprocess
import sys
import time

import spotipy
import yaml
from spotipy.oauth2 import SpotifyOAuth

SCOPES = "user-modify-playback-state user-read-playback-state streaming"


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

    access_token, refresh_token = run_oauth(cfg)

    cfg["spotify"]["refresh_token"] = refresh_token
    save_config(cfg)
    print(f"\nrefresh_token saved to config.yml")

    populate_librespot_cache(cfg, access_token)

    print("\nAuth setup complete!")
    print("You can now run: python3 stream.py")


if __name__ == "__main__":
    main()
