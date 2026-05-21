#!/bin/bash
# smart-sync.sh
# Syncs /mnt/tank/sync -> /mnt/tank/media/Sync only if files have changed.
# Run every 30 minutes via TrueNAS cron.

SYNC_SRC="/mnt/tank/sync"
SYNC_DST="/mnt/tank/media/Sync"
SNAPSHOT="/tmp/.sync_snapshot"

# Generate current state: file paths + modification times
CURRENT=$(find "$SYNC_SRC" -not -name '.*' -type f -printf '%p %T@\n' 2>/dev/null | sort)

# Compare against last snapshot
if [ -f "$SNAPSHOT" ]; then
    LAST=$(cat "$SNAPSHOT")
    if [ "$CURRENT" = "$LAST" ]; then
        echo "$(date): No changes detected, skipping rsync."
        exit 0
    fi
fi

echo "$(date): Changes detected, running rsync..."
chmod -R 775 "$SYNC_SRC" "$SYNC_DST"
rsync -rvtc --delete --exclude='.*' "$SYNC_SRC/" "$SYNC_DST"

# Save new snapshot only if rsync succeeded
if [ $? -eq 0 ]; then
    echo "$CURRENT" > "$SNAPSHOT"
    echo "$(date): Sync complete, snapshot updated."
else
    echo "$(date): rsync failed, snapshot not updated — will retry next run."
fi
