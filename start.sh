#!/bin/bash
cd "$(dirname "$0")"
nohup .venv/bin/python stream.py > /dev/null 2>&1 &
echo $! > spotistream.pid
echo "Started (PID $!)"
