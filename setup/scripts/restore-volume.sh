#!/usr/bin/env bash
# Restore a johnny-five volume from a tar.gz snapshot.
#
# DESTRUCTIVE: wipes the existing volume contents. Asks for confirmation
# unless run with -y.
#
# Usage:
#   ./restore-volume.sh /path/to/j5-volume-YYYYMMDD-HHMMSS.tar.gz
#   ./restore-volume.sh -y /path/to/snapshot.tar.gz   # skip confirmation
#
# Process:
#   1. Stop the johnny-five container if running
#   2. Wipe existing volume contents
#   3. Extract the tarball into the volume
#   4. Restart the container

set -euo pipefail

YES=0
if [ "${1:-}" = "-y" ]; then
    YES=1
    shift
fi

if [ $# -ne 1 ]; then
    echo "Usage: $0 [-y] <snapshot.tar.gz>" >&2
    exit 2
fi

SNAPSHOT="$1"
VOLUME_NAME="${JOHNNY_FIVE_VOLUME:-johnny-five-data}"
CONTAINER_NAME="${JOHNNY_FIVE_CONTAINER:-johnny-five}"

# Sanity checks
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH" >&2
    exit 1
fi

if [ ! -f "$SNAPSHOT" ]; then
    echo "ERROR: snapshot file not found: $SNAPSHOT" >&2
    exit 1
fi

if ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
    echo "Volume '$VOLUME_NAME' does not exist; creating it."
    docker volume create "$VOLUME_NAME"
fi

# Resolve absolute path of snapshot for Docker mount
SNAPSHOT_ABS="$(cd "$(dirname "$SNAPSHOT")" && pwd)/$(basename "$SNAPSHOT")"
SNAPSHOT_DIR="$(dirname "$SNAPSHOT_ABS")"
SNAPSHOT_FILE="$(basename "$SNAPSHOT_ABS")"

echo "About to restore from: $SNAPSHOT_ABS"
echo "Target volume: $VOLUME_NAME"
echo "This will WIPE the current contents of '$VOLUME_NAME'."
echo

if [ $YES -eq 0 ]; then
    printf "Continue? (type 'yes' to proceed): "
    read -r CONFIRM
    if [ "$CONFIRM" != "yes" ]; then
        echo "Aborted."
        exit 0
    fi
fi

# Stop the container if running
if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "Stopping container '$CONTAINER_NAME'..."
    docker stop "$CONTAINER_NAME" >/dev/null
    RESTART_AFTER=1
else
    RESTART_AFTER=0
fi

# Wipe + extract in a one-shot busybox container
echo "Wiping existing volume contents..."
docker run --rm \
    -v "$VOLUME_NAME":/data \
    alpine \
    sh -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true'

echo "Extracting snapshot..."
docker run --rm \
    -v "$VOLUME_NAME":/data \
    -v "$SNAPSHOT_DIR":/backup:ro \
    alpine \
    tar xzf "/backup/$SNAPSHOT_FILE" -C /data

if [ $RESTART_AFTER -eq 1 ]; then
    echo "Starting container '$CONTAINER_NAME'..."
    docker start "$CONTAINER_NAME" >/dev/null
fi

echo
echo "Restore complete. Verify with:"
echo "  docker exec $CONTAINER_NAME python -c \"import asyncio; from claude_memory.mcp.tools import tool_memory_stats; print(asyncio.run(tool_memory_stats()))\""
