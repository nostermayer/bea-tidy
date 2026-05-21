#!/bin/bash
# smart-sync.sh
# Syncs SYNC_SRC -> SYNC_DST only if files have changed.
# Run every 30 minutes via TrueNAS cron.

SYNC_SRC="${SYNC_SRC:-/mnt/tank/sync}"
SYNC_DST="${SYNC_DST:-/mnt/tank/media/Sync}"
# Persistent snapshot — survives reboots unlike /tmp
SNAPSHOT="${SYNC_DST}/.sync_snapshot"
LOCK_FILE="/var/lock/smart-sync.lock"

# Prevent overlapping runs if rsync takes longer than the cron interval
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(date): Another instance is running, exiting."
    exit 0
fi

# Generate current state: file paths + modification times
CURRENT=$(find "$SYNC_SRC" -not -name '.*' -type f -printf '%p %T@\n' 2>/dev/null | sort)

# Safety guard: abort if source appears empty (unmounted drive, Syncthing issue)
FILE_COUNT=$(echo "$CURRENT" | grep -c . || true)
if [ "$FILE_COUNT" -eq 0 ]; then
    echo "$(date): SYNC_SRC appears empty — skipping rsync to avoid wiping destination."
    exit 1
fi

# Compare against last snapshot
if [ -f "$SNAPSHOT" ]; then
    LAST=$(cat "$SNAPSHOT")
    if [ "$CURRENT" = "$LAST" ]; then
        echo "$(date): No changes detected, skipping rsync."
        exit 0
    fi
fi

echo "$(date): Changes detected, running rsync..."
chmod -R 775 "$SYNC_DST"
rsync -rvtc --delete --exclude='.*' "$SYNC_SRC/" "$SYNC_DST"

if [ $? -eq 0 ]; then
    echo "$CURRENT" > "$SNAPSHOT"
    echo "$(date): Sync complete, snapshot updated."
else
    echo "$(date): rsync failed, snapshot not updated — will retry next run."
fi
