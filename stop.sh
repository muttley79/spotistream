#!/bin/bash
cd "$(dirname "$0")"
PID_FILE=radio-monitor.pid

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found, is the monitor running?"
    exit 1
fi

kill $(cat "$PID_FILE") && rm "$PID_FILE"
echo "Stopped"
