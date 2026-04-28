#!/usr/bin/env bash
# Snapshot the johnny-five Docker volume to a tar.gz file.
# Live-safe: mounts the volume read-only, so the running container is unaffected.
#
# Usage:
#   ./backup-volume.sh                   # writes to ./j5-backups/
#   ./backup-volume.sh /custom/path      # writes to /custom/path
#
# The output filename includes a UTC timestamp, e.g.
#   j5-volume-20260428-103045.tar.gz

set -euo pipefail

DEFAULT_DIR="./j5-backups"
TARGET_DIR="${1:-$DEFAULT_DIR}"
VOLUME_NAME="${JOHNNY_FIVE_VOLUME:-johnny-five-data}"
TIMESTAMP="$(date -u +%Y%m%d-%H%M%S)"
FILENAME="j5-volume-${TIMESTAMP}.tar.gz"

# Sanity checks
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker not found in PATH" >&2
    exit 1
fi

if ! docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1; then
    echo "ERROR: docker volume '$VOLUME_NAME' does not exist" >&2
    echo "       (set JOHNNY_FIVE_VOLUME if your volume has a different name)" >&2
    exit 1
fi

mkdir -p "$TARGET_DIR"
TARGET_DIR_ABS="$(cd "$TARGET_DIR" && pwd)"

echo "Snapshotting volume '$VOLUME_NAME' → $TARGET_DIR_ABS/$FILENAME"

docker run --rm \
    -v "$VOLUME_NAME":/data:ro \
    -v "$TARGET_DIR_ABS":/backup \
    alpine \
    tar czf "/backup/$FILENAME" -C /data .

SIZE_HUMAN="$(du -h "$TARGET_DIR_ABS/$FILENAME" | cut -f1)"
echo "Backup written: $TARGET_DIR_ABS/$FILENAME ($SIZE_HUMAN)"

# Print restore hint
cat <<EOF

To restore this snapshot later:
  ./setup/scripts/restore-volume.sh "$TARGET_DIR_ABS/$FILENAME"
EOF
