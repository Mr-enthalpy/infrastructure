# State backup, restore, and failure alerts

This runbook covers the P0 operational layer: the broker SQLite state, the
root-owned configuration/key tree, the intake database, and failure
notification. It does not back up member workspaces or Docker image layers;
those follow their own quota/retention policy.

## What is backed up

`citespec-resource-backup` creates a timestamped directory under
`/var/backups/citespec-resource/` (mode `0700`) containing:

- `state.db`: a consistent copy of `server.state_db` made with SQLite's online
  backup API;
- `intake.db`: the intake database when `onboarding.intake_db` exists;
- `etc/<config-dir>/`: the complete root-owned configuration directory,
  including `/etc/citespec-resource/authorized-keys.d/`;
- `manifest.json`: schema version, source paths, file sizes, and SHA-256
  checksums.

The backup is written to a temporary directory, verified, then atomically
renamed. The service keeps the newest 14 `state-*` backups; older entries are
removed only when they match the generated name and contain a manifest.
Pre-cutover/acceptance directories are not touched.

## Daily backup

```bash
systemctl list-timers citespec-resource-backup.timer
systemctl start citespec-resource-backup.service
journalctl -u citespec-resource-backup.service -n 100 --no-pager
```

A failed backup triggers `citespec-resource-alert@...service`. The timer can be
disabled during installation with `install_server.sh --no-timers`.

## Verify a backup

```bash
/opt/citespec-resource/.venv/bin/python -m citespec_resource.backup verify \
  /var/backups/citespec-resource/state-YYYYMMDD-HHMMSS-PID
```

Verification checks every manifest checksum and runs SQLite
`PRAGMA integrity_check` on both databases.

## Restore

Restore is an explicit maintenance operation. Stop the broker first; the
restore command refuses to replace live state while
`citespec-resource-broker.service` is active.

```bash
sudo systemctl stop citespec-resource-broker.service
sudo /opt/citespec-resource/.venv/bin/python -m citespec_resource.backup restore \
  /var/backups/citespec-resource/state-YYYYMMDD-HHMMSS-PID \
  --config /etc/citespec-resource/config.toml \
  --yes
sudo systemctl start citespec-resource-broker.service
```

The command verifies the backup, creates a `pre-restore-*` safety backup when
the target database exists, replaces the database atomically, and removes stale
`-wal`/`-shm` sidecars. Add `--full` only when the configuration/key tree must
also be restored; review the old config paths first.

After restore, run the read-only checks:

```bash
/usr/local/libexec/citespec-resource/verify_gpu_permissions.sh --json
citespec-resource status --json
```

## Alerts

The installer installs `citespec-resource-alert@.service` and wires
`OnFailure=` for the broker, intake, disk check, log rotation, and backup
units. The alert script always writes `/var/log/citespec-resource/alerts.log`
and the journal. Optional webhook delivery is configured in a root-owned file:

```bash
sudo install -m 0600 /dev/null /etc/citespec-resource/alert.env
sudo tee /etc/citespec-resource/alert.env >/dev/null <<'EOF'
CITESPEC_ALERT_WEBHOOK=https://example.invalid/citespec-alert
EOF
```

Test the local path without a webhook:

```bash
/usr/local/sbin/citespec-alert citespec-resource-backup.service
tail -n 5 /var/log/citespec-resource/alerts.log
```

The webhook receives a JSON body with the failed unit, host, and UTC timestamp.
A webhook delivery failure returns non-zero and remains visible in the alert
service journal; it does not recursively invoke another alert unit.
