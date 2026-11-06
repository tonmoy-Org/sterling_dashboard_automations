#!/bin/bash
set -e

# Clean up any stale Xvfb lock
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# Start Xvfb virtual framebuffer on display :99 in background
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp -nolisten unix > /dev/null 2>&1 &
XVFB_PID=$!

export DISPLAY=:99

# Graceful cleanup on termination
trap "kill -TERM $XVFB_PID 2>/dev/null || true" EXIT TERM INT

# Execute container command
exec "$@"

