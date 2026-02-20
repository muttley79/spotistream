# Spotistream

A Spotify playlist HTTP audio streaming server. A playlist streams as a radio-style HTTP stream openable in VLC. Multiple listeners share the same stream. Playback only runs on Spotify when at least one client is connected.

## How it works

- First client connects → playback starts at a random track and position
- Additional clients join the existing stream (ring buffer pre-fills them seamlessly)
- Last client disconnects → Spotify is paused
- End of playlist → automatically loops back to track 0
- **Promos (optional):** a random MP3 from a local folder is injected between songs every 3–5 tracks

## Requirements

### System dependencies

- **librespot** — [github.com/librespot-org/librespot](https://github.com/librespot-org/librespot)

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

Or using the bundled venv:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
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

Edit `config.yml` with your credentials and preferences:

```yaml
spotify:
  client_id: YOUR_CLIENT_ID
  client_secret: YOUR_CLIENT_SECRET
  playlist_id: YOUR_PLAYLIST_ID   # e.g. 37i9dQZF1DXcBWIGoYBM5M

librespot:
  device_name: Spotistream
  cache_dir: /home/pi/.cache/librespot
  bitrate: 320
  username: ""          # optional, helps librespot find the right cache entry
  path: ""              # optional: full path if librespot is not on PATH
  initial_volume: 100   # 0–100

ffmpeg:
  path: ""              # optional: full path if ffmpeg is not on PATH

server:
  port: 8000
  buffer_secs: 10       # seconds of audio pre-buffered for late-joining clients
  promos_dir: ""        # optional: path to folder of .mp3 promo files
  auth_user: ""         # optional: Basic Auth username (leave blank to disable)
  auth_password: ""     # optional: Basic Auth password

logging:
  log_file: spotistream.log
  max_bytes: 10485760   # 10 MB per rotated file
  backup_count: 3
```

### 3. Auth (one time)

```bash
python3 auth_setup.py
```

This will:
1. Print a Spotify authorization URL — open it in any browser (laptop/phone)
2. After authorizing, paste the redirect URL back into the terminal
3. Save the `refresh_token` into `config.yml`
4. Bootstrap the librespot credential cache

## Running

### Directly

```bash
python3 stream.py
# or with venv:
.venv/bin/python stream.py
```

The config file path defaults to `config.yml` in the working directory. Override with the `CONFIG` environment variable:

```bash
CONFIG=/path/to/config.yml python3 stream.py
```

### With start/stop scripts

```bash
./start.sh   # starts in background, writes PID to spotistream.pid
./stop.sh    # gracefully stops the background process
```

### As a systemd service

Copy the included unit file, edit the `User` and `WorkingDirectory` paths to match your setup, then enable it:

```bash
sudo cp spotistream.service /etc/systemd/system/
sudo nano /etc/systemd/system/spotistream.service   # update User and WorkingDirectory
sudo systemctl daemon-reload
sudo systemctl enable spotistream
sudo systemctl start spotistream
```

Check status and logs:
```bash
sudo systemctl status spotistream
journalctl -u spotistream -f
```

Logs are also written to `spotistream.log` (configured under `logging.log_file`).

### Connecting

```bash
vlc http://pi:8000/stream
# with Basic Auth enabled:
vlc http://user:password@pi:8000/stream
```

Health check (no credentials required):
```bash
curl http://pi:8000/health
```

## Promos

Drop MP3 files into a folder (e.g. `promos/`) and set `promos_dir` in `config.yml`:

```yaml
server:
  promos_dir: promos
```

**How it works:**
- Every 3–5 songs (randomised), Spotistream pauses Spotify, injects a random promo MP3 directly into the live stream, then resumes Spotify playback.
- Clients hear: the start of the new song → promo → song resumes.
- Listeners who connect mid-promo receive it normally (it's in the ring buffer like any other audio).
- If no clients are connected, the promo counter still advances but no promo is injected.
- Feature is entirely opt-in — if `promos_dir` is unset or the directory doesn't exist, behaviour is identical to having no promos configured.

**Promo file requirements:**
- Format: MP3
- Recommended bitrate: **192 kbps** (matches the stream output bitrate for accurate rate-limiting)
- Other bitrates work; only the sleep pacing is slightly off — audio still plays correctly

## Configuration reference

| Key | Description | Default |
|---|---|---|
| `spotify.client_id` | Spotify app client ID | required |
| `spotify.client_secret` | Spotify app client secret | required |
| `spotify.refresh_token` | Written by `auth_setup.py` | required |
| `spotify.playlist_id` | Spotify playlist ID to stream | required |
| `librespot.device_name` | Name shown in Spotify Connect | `Spotistream` |
| `librespot.cache_dir` | librespot credential cache path | required |
| `librespot.bitrate` | librespot input bitrate (96/160/320) | `320` |
| `librespot.username` | Spotify username (optional, helps find cache) | `""` |
| `librespot.path` | Full path to librespot binary (falls back to PATH) | `""` |
| `librespot.initial_volume` | Initial playback volume (0–100) | `100` |
| `ffmpeg.path` | Full path to ffmpeg binary (falls back to PATH) | `""` |
| `server.port` | HTTP server port | `8000` |
| `server.buffer_secs` | Seconds of audio pre-buffered for late-joining clients | `10` |
| `server.promos_dir` | Path to folder of `.mp3` promo files (optional) | `""` |
| `server.auth_user` | HTTP Basic Auth username — leave blank to disable auth | `""` |
| `server.auth_password` | HTTP Basic Auth password — leave blank to disable auth | `""` |
| `logging.log_file` | Log file path | `spotistream.log` |
| `logging.max_bytes` | Max log file size before rotation | `10485760` |
| `logging.backup_count` | Number of rotated log files to keep | `3` |

## Files

```
spotistream/
├── config.example.yml   # config template (no secrets)
├── config.yml           # your config (gitignore this)
├── requirements.txt
├── auth_setup.py        # one-time auth setup
├── stream.py            # main server
├── start.sh             # start server in background (writes spotistream.pid)
├── stop.sh              # stop background server
├── spotistream.service  # systemd unit file (edit paths before use)
└── promos/              # optional: place .mp3 promo files here
```
