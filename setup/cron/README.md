# Scheduled backups for johnny-five

Examples for running [`backup-volume.sh`](../scripts/backup-volume.sh) automatically. Pick the path that matches your platform.

The volume snapshot is small (~1KB per memory) and the script is live-safe (read-only mount), so a daily or weekly schedule is fine for most setups. Most users: weekly local + monthly off-site copy.

---

## Linux / macOS — cron

Edit your user crontab:

```bash
crontab -e
```

Add one of:

```cron
# Daily at 03:00 — keeps last 30 days of snapshots
0 3 * * * /path/to/johnny-five/setup/scripts/backup-volume.sh ~/j5-backups/daily >> ~/j5-backups/cron.log 2>&1

# Weekly Sunday 03:00
0 3 * * 0 /path/to/johnny-five/setup/scripts/backup-volume.sh ~/j5-backups/weekly >> ~/j5-backups/cron.log 2>&1

# Monthly first day at 03:00
0 3 1 * * /path/to/johnny-five/setup/scripts/backup-volume.sh ~/j5-backups/monthly >> ~/j5-backups/cron.log 2>&1
```

**Important**: cron has a minimal `PATH`. The `backup-volume.sh` script needs `docker` on PATH. Either:

1. Set `PATH` at the top of the crontab:
   ```cron
   PATH=/usr/local/bin:/usr/bin:/bin
   ```

2. Or call docker by absolute path — find it with `which docker` and edit the script (less portable).

---

## Linux — systemd timer (alternative to cron)

Modern Linux: prefer systemd timers over cron. Persistent across reboots, includes the run if the machine was off at the scheduled time.

Create `~/.config/systemd/user/j5-backup.service`:

```ini
[Unit]
Description=Johnny-Five memory backup

[Service]
Type=oneshot
ExecStart=/path/to/johnny-five/setup/scripts/backup-volume.sh %h/j5-backups
StandardOutput=append:%h/j5-backups/systemd.log
StandardError=append:%h/j5-backups/systemd.log
```

Create `~/.config/systemd/user/j5-backup.timer`:

```ini
[Unit]
Description=Daily Johnny-Five backup at 03:00

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
systemctl --user daemon-reload
systemctl --user enable --now j5-backup.timer
systemctl --user list-timers | grep j5-backup
```

---

## macOS — launchd

`launchd` is the native macOS scheduler. cron also works on macOS but launchd is more robust on laptops that go to sleep.

Create `~/Library/LaunchAgents/com.johnnyfive.backup.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.johnnyfive.backup</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/path/to/johnny-five/setup/scripts/backup-volume.sh</string>
        <string>/Users/YOUR_USER/j5-backups</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USER/j5-backups/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USER/j5-backups/launchd.log</string>
</dict>
</plist>
```

(Replace `YOUR_USER` and the script path with your actuals.)

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.johnnyfive.backup.plist
launchctl list | grep johnnyfive
```

To unload: `launchctl unload ~/Library/LaunchAgents/com.johnnyfive.backup.plist`.

If your Mac is asleep at the scheduled time, launchd runs the job at next wake.

---

## Windows — Task Scheduler

Task Scheduler can run the bash script via Git Bash (recommended) or WSL.

### Via Git Bash

1. Open **Task Scheduler** → **Create Task...**
2. **General** tab:
   - Name: `johnny-five backup`
   - Run whether user is logged on or not (for unattended runs)
3. **Triggers** tab → **New...**:
   - Daily at 03:00 (or your preference)
4. **Actions** tab → **New...**:
   - Action: Start a program
   - Program/script: `C:\Program Files\Git\bin\bash.exe`
   - Arguments: `-c "/c/path/to/johnny-five/setup/scripts/backup-volume.sh /c/Users/YOU/j5-backups"`
5. **Settings** tab:
   - Allow task to be run on demand (for testing)
   - If task fails, restart every 5 minutes (up to 3 times)

Test the trigger immediately by right-clicking the task → **Run**.

### Via WSL

If you prefer WSL:

- Action → Program/script: `wsl`
- Arguments: `bash -c "/mnt/c/path/to/johnny-five/setup/scripts/backup-volume.sh /mnt/c/Users/YOU/j5-backups"`

### PowerShell-native (no bash)

If you don't want to depend on Git Bash or WSL, here's a PowerShell wrapper that calls Docker directly:

```powershell
# C:\Scripts\j5-backup.ps1
$ErrorActionPreference = 'Stop'
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$targetDir = "$env:USERPROFILE\j5-backups"
$filename  = "j5-volume-$timestamp.tar.gz"

New-Item -ItemType Directory -Force -Path $targetDir | Out-Null

docker run --rm `
  -v johnny-five-data:/data:ro `
  -v "${targetDir}:/backup" `
  alpine `
  tar czf "/backup/$filename" -C /data .

if ($LASTEXITCODE -ne 0) { throw "Backup failed" }

$size = (Get-Item "$targetDir\$filename").Length / 1KB
Write-Host "Backup written: $targetDir\$filename ($size KB)"
```

Schedule that PowerShell script via Task Scheduler with action:

- Program/script: `powershell.exe`
- Arguments: `-NoProfile -ExecutionPolicy Bypass -File C:\Scripts\j5-backup.ps1`

---

## Rotation (keep N most recent, delete older)

Cron (or any scheduler) can call this rotation snippet after the backup:

`setup/scripts/rotate-backups.sh` (write yourself, ~10 lines):

```bash
#!/usr/bin/env bash
# Keep the N most recent j5-volume-*.tar.gz in $1; delete older.
set -euo pipefail
DIR="${1:?usage: rotate-backups.sh <dir> [keep_count=14]}"
KEEP="${2:-14}"
ls -1t "$DIR"/j5-volume-*.tar.gz 2>/dev/null \
    | tail -n +$((KEEP + 1)) \
    | xargs -r rm -v
```

Then chain: `backup-volume.sh ... && rotate-backups.sh ~/j5-backups 14`

For systemd / launchd / Task Scheduler, add a second action that calls the rotation script.

---

## Off-site backup (cloud sync)

The simplest off-site strategy: point the backup target at a cloud-synced folder.

| Platform | Sync folder examples |
|---|---|
| Dropbox | `~/Dropbox/j5-backups/` |
| iCloud Drive (macOS) | `~/Library/Mobile Documents/com~apple~CloudDocs/j5-backups/` |
| OneDrive | `~/OneDrive/j5-backups/` |
| Google Drive (Drive for desktop) | `~/Google Drive/My Drive/j5-backups/` |

**Caveats:**
- Cloud providers see the contents. If memories include sensitive context, encrypt first (see "Encryption" in [`docs/BACKUP_AND_RESTORE.md`](../../docs/BACKUP_AND_RESTORE.md)).
- Some clients aggressively dedupe files — fine for tarballs, just be aware.
- Consider a separate cloud account if your work memories shouldn't mix with personal sync.

For S3 / GCS / Azure: the backup script writes locally, then a follow-up command uploads (`aws s3 cp`, `gsutil cp`, `azcopy`). Not bundled in this repo because cloud auth is per-environment.

---

## Verifying scheduled backups actually run

A backup that never runs is worse than no backup — you have a false sense of security. Verify:

1. Check the log file written by the scheduler.
2. List the backup target directory periodically: `ls -lht ~/j5-backups/ | head`.
3. Set up a calendar reminder once a month to test a restore from the most recent snapshot (without actually wiping the live volume — restore to a test volume name).

If you go a month with no new files in the backup target, your scheduler isn't running. Check logs.
