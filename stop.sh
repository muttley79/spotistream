#!/bin/bash
cd "$(dirname "$0")"
PID_FILE=spotistream.pid

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found, is spotistream running?"
    exit 1
fi

MAIN_PID=$(cat "$PID_FILE")

# Kill child processes (librespot, ffmpeg) before the parent exits
pkill -P "$MAIN_PID" 2>/dev/null

if kill "$MAIN_PID" 2>/dev/null; then
    rm "$PID_FILE"
    echo "Stopped (PID $MAIN_PID)"
else
    echo "Process $MAIN_PID not found (stale PID file)"
    rm "$PID_FILE"
    exit 1
fi
