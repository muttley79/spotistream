# Spotistream

A Spotify playlist HTTP audio streaming server. A playlist streams as a radio-style HTTP stream openable in VLC. Multiple listeners share the same stream. Playback only runs on Spotify when at least one client is connected.

## How it works

- First client connects → playback starts at a random track offset
- Additional clients join the existing stream (no extra Spotify API calls)
- Last client disconnects → Spotify is paused
- End of playlist → automatically loops back to track 0

## Requirements

### System dependencies

- **librespot** — [github.com/librespot-org/librespot](https://github.com/librespot-org/librespot)

  The librespot GitHub releases page ships source only. Options:

  **Via cargo:**
  ```bash
  cargo install librespot --locked
  ```

  **Pre-built binary (Raspberry Pi) — extract from raspotify's .deb without installing the service:**
  ```bash
  # arm64 (aarch64):
  wget https://github.com/dtcooper/raspotify/releases/download/0.48.1/raspotify_0.48.1.librespot.v0.8.0-ea81314_arm64.deb
  dpkg-deb -x raspotify_0.48.1.librespot.v0.8.0-ea81314_arm64.deb /tmp/rsp
  sudo cp /tmp/rsp/usr/bin/librespot /usr/local/bin/librespot

  # armhf (32-bit):
  wget https://github.com/dtcooper/raspotify/releases/download/0.48.1/raspotify_0.48.1.librespot.v0.8.0-ea81314_armhf.deb
  dpkg-deb -x raspotify_0.48.1.librespot.v0.8.0-ea81314_armhf.deb /tmp/rsp
  sudo cp /tmp/rsp/usr/bin/librespot /usr/local/bin/librespot
  ```

  If the binary is not on PATH, set `librespot.path` in `config.yml`.
- **ffmpeg**
  ```bash
  sudo apt install ffmpeg
  ```

### Python dependencies

```bash
pip install -r requirements.txt
```

## Setup

### 1. Spotify app credentials

Create an app at [developer.spotify.com](https://developer.spotify.com/dashboard):
- Add `http://localhost:8888/callback` as a Redirect URI
- Note down the **Client ID** and **Client Secret**

### 2. Config

```bash
cp config.example.yml config.yml
```

Edit `config.yml`:
```yaml
spotify:
  client_id: YOUR_CLIENT_ID
  client_secret: YOUR_CLIENT_SECRET
  playlist_id: YOUR_PLAYLIST_ID   # e.g. 37i9dQZF1DXcBWIGoYBM5M

librespot:
  device_name: Spotistream
  cache_dir: /home/pi/.cache/librespot
  bitrate: 320
  path: ""      # optional: full path if librespot is not on PATH

ffmpeg:
  path: ""      # optional: full path if ffmpeg is not on PATH

server:
  port: 8000
```

### 3. Auth (one time)

```bash
python3 auth_setup.py
```

This will:
1. Print a Spotify authorization URL — open it on any browser (laptop/phone)
2. After authorizing, paste the redirect URL back into the terminal
3. Save the refresh token into `config.yml`
4. Bootstrap the librespot credential cache

## Running

```bash
python3 stream.py
```

Connect a listener:
```bash
vlc http://pi:8000/stream
```

Health check:
```bash
curl http://pi:8000/health
```

## Configuration reference

| Key | Description | Default |
|---|---|---|
| `spotify.client_id` | Spotify app client ID | required |
| `spotify.client_secret` | Spotify app client secret | required |
| `spotify.refresh_token` | Written by `auth_setup.py` | required |
| `spotify.playlist_id` | Spotify playlist ID to stream | required |
| `librespot.device_name` | Name shown in Spotify Connect | `Spotistream` |
| `librespot.cache_dir` | librespot credential cache path | required |
| `librespot.bitrate` | librespot bitrate (96/160/320) | `320` |
| `librespot.username` | Spotify username (optional, helps librespot find cache) | `""` |
| `librespot.path` | Full path to librespot binary (falls back to PATH) | `""` |
| `ffmpeg.path` | Full path to ffmpeg binary (falls back to PATH) | `""` |
| `server.port` | HTTP server port | `8000` |

The config path can be overridden via the `CONFIG` environment variable:
```bash
CONFIG=/path/to/config.yml python3 stream.py
```

## Files

```
spotistream/
├── config.example.yml  # config template (no secrets)
├── config.yml          # your config (gitignore this)
├── requirements.txt
├── auth_setup.py       # one-time auth setup
└── stream.py           # main server
```
