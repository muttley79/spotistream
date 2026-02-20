#!/bin/bash
cd "$(dirname "$0")"
PID_FILE=spotistream.pid

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "Already running (PID $(cat "$PID_FILE"))"
    exit 1
fi

nohup .venv/bin/python stream.py > /dev/null 2>&1 &
echo $! > "$PID_FILE"
echo "Started (PID $!)"
