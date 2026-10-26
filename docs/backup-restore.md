# State backup, restore, and failure alerts

This runbook covers the P0 operational layer: the broker SQLite state, the
root-owned configuration/key tree, the intake database, the provisioning
spool, and failure notification. It does not back up member workspaces or
Docker image layers; those follow their own quota/retention policy.

## What is backed up

`citespec-resource-backup` creates a timestamped directory under
`/var/backups/citespec-resource/` (mode `0700`) containing:

- `state.db`: a consistent copy of `server.state_db` made with SQLite's online
  backup API;
- `intake.db`: the intake database when `onboarding.intake_db` is configured.
  A configured but missing database fails the backup;
- `etc/<config-dir>/`: the complete root-owned configuration directory,
  including `/etc/citespec-resource/authorized-keys.d/`;
- `provision/`: the complete provisioning spool, including `requests/`,
  `processing/`, `results/`, and the executor lock file;
- `manifest.json`: schema version, source paths, file sizes, and SHA-256
  checksums.

Backup schema 2 is the supported recovery format. It includes the intake and
provisioning state so a restored broker cannot silently diverge from member
keys or a completed operation. The backup is written to a temporary directory,
verified, then atomically renamed. The service keeps the newest 14 `state-*`
backups; older entries are removed only when they match the generated name and
contain a manifest. Pre-cutover/acceptance directories are not touched.

## Restore safety contract

A restore is a root-only maintenance operation. The command:

1. verifies every manifest checksum and both SQLite databases;
2. stops and re-checks `citespec-resource-provision.path`,
   `citespec-resource-provision.service`, `citespec-resource-intake.service`,
   and `citespec-resource-broker.service`;
3. forces the current state database to `maintenance` before creating the
   safety backup;
4. stages and verifies the databases, config tree, and spool before replacing
   any live path;
5. swaps paths atomically and rolls every completed swap back on failure; and
6. forces the restored state to `maintenance` and re-runs SQLite integrity
   checks.

The restore command does not restart services. Start them only after the mode
and data checks below pass; the broker therefore cannot reopen active by
accident.

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

Verification checks every manifest checksum, requires the configured intake
database and provisioning spool directories, and runs SQLite
`PRAGMA integrity_check` on both databases.

## Restore

Run the restore as root. It stops the writer units itself; do not start a
second restore or a broker while it is running.

```bash
sudo /opt/citespec-resource/.venv/bin/python -m citespec_resource.backup restore \
  /var/backups/citespec-resource/state-YYYYMMDD-HHMMSS-PID \
  --config /etc/citespec-resource/config.toml \
  --yes
```

The default restore replaces `state.db`, the configured `intake.db`, and the
provisioning spool. It also creates a `pre-restore-*` safety backup in the
backup root and removes stale `-wal`/`-shm` sidecars. The intake database is
restored with the ownership of the existing database (or its state directory),
so the low-privilege intake service can still open it.

Add `--full` only when the configuration/key tree must also be restored. The
full mode replaces the whole `etc/<config-dir>/` tree exactly: files added
after the backup are removed. The restored `config.toml` must still point at
the same state, intake, and spool paths, otherwise the restore aborts before
swapping live paths.

`--skip-service-check` is only for a manually isolated host or recovery media
where systemd is unavailable. Stop all four writer units and confirm they are
inactive before using it.

On failure, completed path swaps are rolled back and the original paths remain
in place. The current state may already have been forced to `maintenance`; that
is intentional fail-closed behavior. Investigate before retrying.

After a successful restore, start the services in dependency order and run the
read-only checks:

```bash
sudo systemctl start citespec-resource-provision.path
sudo systemctl start citespec-resource-intake.service
sudo systemctl start citespec-resource-broker.service
/usr/local/libexec/citespec-resource/verify_gpu_permissions.sh --json
citespec-resource status --json
```

The restored broker starts in `maintenance`. Confirm member keys, queued jobs,
allocations, and intake records, then switch mode explicitly:

```bash
citespec-resource admin mode active
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
