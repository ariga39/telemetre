# telemetre operations guide

This guide covers slice three: restricted SSH delivery, scheduled and single-instance jobs, expected-source health checks, and online backup/restore of the centre SQLite database. For the field rules used by collection, monthly reports, and quota sampling, see `docs/DESIGN.md`, `docs/agy-usage.md`, and `docs/quota.md`.

All commands use only the Python standard library and the system `ssh`. Binding files, host public-key files, and identity files contain no model credentials; do not write OAuth tokens, API keys, private-key material, or raw provider output into any file discussed here.

## 1. Hosts and directories

Collection and quota sampling must run as the user who is actually logged in to agy/Codex on that machine, preserving that user's writable home, login state, and CLI path. Do not run source-side jobs as the centre's restricted service user.

Source-side user (example user `alpha`):

```text
~/.gemini/antigravity-cli                          agy 1.5.16 data root
~/.local/state/telemetre/client.sqlite3            usage pending queue
~/.local/state/telemetre/quota.sqlite3             local quota database
~/.config/telemetre/telemetre.env                  machine/account/binding ID/SSH target
~/.config/telemetre/bindings.json                  binding copy used for client-side validation
~/.config/telemetre/quota.json                     local agy/Codex sampling configuration
~/.ssh/telemetre_usage                             dedicated key bound to alpha-usage
~/.ssh/telemetre_quota_agy                         dedicated key bound to alpha-quota-agy
~/.ssh/telemetre_quota_codex                       dedicated key bound to alpha-quota-codex
~/.ssh/telemetre_known_hosts                       pinned centre host public key
```

Centre (runs only the restricted service user `telemetre`):

```text
/etc/telemetre/bindings.json         authoritative binding table (root:telemetre 0640)
/etc/telemetre/center.env            centre database/backup/health-check paths
/etc/telemetre/quota-center.json     centre daily-summary timezone and both providers
/var/lib/telemetre/center.sqlite3    the centre's single SQLite database
/var/backups/telemetre/              online backup snapshots
```

Each client machine has its own stable `machine` alias and account alias. Each SSH public key binds to exactly one identity at the centre; usage, agy quota, and Codex quota use different keys and different binding IDs. Quota write permission is granted only to the designated sampling machine.

## 2. Centre bindings and forced commands

See `examples/bindings.example.json` for a `bindings.json` example. Keys are binding IDs and values contain only:

```json
{
  "bindings": {
    "alpha-usage": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "agy_usage"
    },
    "alpha-quota-agy": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "quota_sample",
      "provider": "agy"
    },
    "alpha-quota-codex": {
      "machine": "alpha",
      "account": "personal@example.test",
      "kind": "quota_sample",
      "provider": "codex"
    }
  }
}
```

The server takes `machine/account/kind/provider` from the authenticated binding; fields with the same names in the request body are never used. `quota_sample` must set `provider`; `agy_usage` must not. The source-side `~/.config/telemetre/bindings.json` only needs the entries used by that machine and must stay consistent with the centre copy; the centre file is authoritative for authorization.

The centre installs each public key's own forced command in the `authorized_keys` file of the restricted user `telemetre`. Example:

```text
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-usage --center-db /var/lib/telemetre/center.sqlite3 --max-rows 1000 --max-bytes 262144" ssh-ed25519 AAAA... alpha-usage
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-quota-agy --center-db /var/lib/telemetre/center.sqlite3 --max-bytes 262144" ssh-ed25519 AAAA... alpha-quota-agy
restrict,command="/usr/bin/python3 -m telemetre.delivery receive --bindings /etc/telemetre/bindings.json --binding-id alpha-quota-codex --center-db /var/lib/telemetre/center.sqlite3 --max-bytes 262144" ssh-ed25519 AAAA... alpha-quota-codex
```

`restrict` disables PTY, port forwarding, agent forwarding, X11, and user rc; no forwarding needs to be enabled separately. The remote command sent by the client is not executed; the key's `command=` is.

At minimum, tighten `/etc/ssh/sshd_config` to:

```text
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
AllowUsers telemetre
PermitTTY no
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PermitUserRC no
AuthenticationMethods publickey
MaxAuthTries 2
```

The `telemetre` user must be able to execute the forced command, so its shell is `/bin/sh`, but it accepts only the restricted public keys above. Database directories are owned by `telemetre`:

```bash
install -d -o telemetre -g telemetre -m 0750 /var/lib/telemetre /var/backups/telemetre
install -d -o root -g telemetre -m 0750 /etc/telemetre
chown telemetre:telemetre /var/lib/telemetre/center.sqlite3 2>/dev/null || true
chmod 0640 /etc/telemetre/bindings.json /etc/telemetre/center.env
```

## 3. Client configuration

Generate three dedicated keys under the ordinary user that actually uses agy/Codex on that machine; one key maps to one binding and one provider:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_usage
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_quota_agy
ssh-keygen -t ed25519 -N '' -f ~/.ssh/telemetre_quota_codex
```

Give the public keys to the centre administrator and write them into the `authorized_keys` lines for `alpha-usage`, `alpha-quota-agy`, and `alpha-quota-codex`. Private keys must be mode `0600`; `send`/`send-quota` reject identity files readable or writable by group or others.

The agy 1.5.16 data root is `~/.gemini/antigravity-cli`, and `agy -p /usage` also writes to that user's login/log directory, so source-side jobs must run as that user and be able to write its own home. Codex's `app-server` also uses that user's login state. The systemd examples are therefore **user units**; do not apply the centre's `ProtectHome=read-only` or the `telemetre` service user to them. If a CLI is not on the default PATH, edit the user unit's `Environment=PATH=...`, for example by putting an nvm node bin directory or `~/.local/bin` first.

Pin the centre host public key: verify the fingerprint out of band first, then write the key line into `known_hosts`. For a non-22 port, write the host as `[centre.example.test]:2222`:

```text
centre.example.test ssh-ed25519 AAAAC3Nza...
```

Clients pass `--known-hosts` and do not rely on `~/.ssh/config`: delivery commands always use `-F /dev/null`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=<specified file>`, `GlobalKnownHostsFile=/dev/null`, `BatchMode=yes`, and `IdentitiesOnly=yes`, and disable all forwarding. Do not set a passphrase on identity files; scheduled jobs have no interactive input.

The source path template is `examples/telemetre.user.env.example` (copy to `~/.config/telemetre/telemetre.env`); the centre path template is `examples/telemetre.center.env.example` (copy to `/etc/telemetre/center.env`). Manual collection and delivery:

```bash
export PYTHONPATH=/opt/telemetre

python3 -m telemetre collect \
  --client-db ~/.local/state/telemetre/client.sqlite3 \
  --machine alpha --account personal@example.test \
  --root ~/.gemini/antigravity-cli

python3 -m telemetre.delivery send \
  --client-db ~/.local/state/telemetre/client.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-usage \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_usage \
  --known-hosts ~/.ssh/telemetre_known_hosts
```

Quota sampling and delivery, with agy and Codex each using their own key and binding:

```bash
python3 -m telemetre.quota sample --config ~/.config/telemetre/quota.json --if-due

python3 -m telemetre.delivery send-quota \
  --quota-db ~/.local/state/telemetre/quota.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-quota-agy \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_quota_agy \
  --known-hosts ~/.ssh/telemetre_known_hosts

python3 -m telemetre.delivery send-quota \
  --quota-db ~/.local/state/telemetre/quota.sqlite3 \
  --bindings ~/.config/telemetre/bindings.json \
  --binding-id alpha-quota-codex \
  --ssh-host centre.example.test --ssh-user telemetre \
  --identity ~/.ssh/telemetre_quota_codex \
  --known-hosts ~/.ssh/telemetre_known_hosts
```

One quota request carries exactly one sample, and the centre ACKs only after the single-sample transaction commits. On timeout or a lost ACK, retransmit the same `sample_id`; the centre is idempotent by sample ID. One local quota database may contain samples for multiple machines, providers, or accounts at the same time; each `send-quota` synchronizes and attempts only samples for the current binding's machine/provider/account, leaving other samples pending.

`--max-bytes` must not exceed the value in the server forced command. The default is 262144 bytes; `send` defaults to at most 1000 agy facts per run, shrinks the batch automatically when over the limit, and retains the queue with an error when a single fact does not fit. Before opening SSH, `send` compares the client spool's stored machine/account with the selected binding; a wrong binding sends nothing and leaves the centre untouched.

## 4. Scheduling, single instances, and hard timeouts

`examples/systemd/` distinguishes source-side user units from centre system units:

Source side (run as the user actually logged in to agy/Codex):

- `telemetre-collect.service/.timer`: collect `~/.gemini/antigravity-cli` hourly and deliver the usage queue.
- `telemetre-quota.service/.timer`: sample at each source's interval every 15 minutes; agy and Codex each deliver with their own key/binding.

Centre (runs only the restricted user `telemetre`):

- `telemetre-backup.service/.timer`: online backup once a day.
- `telemetre-health.service/.timer`: check expected sources every 30 minutes.
- `telemetre-quota-daily.service/.timer`: generate "yesterday's" quota summary daily at 00:17; the timezone and both providers come from `quota-center.json`.

Source-side installation:

```bash
mkdir -p ~/.config/systemd/user ~/.config/telemetre
cp examples/systemd/telemetre-collect.service examples/systemd/telemetre-collect.timer \
   examples/systemd/telemetre-quota.service examples/systemd/telemetre-quota.timer \
   ~/.config/systemd/user/
install -m 0600 examples/telemetre.user.env.example ~/.config/telemetre/telemetre.env
install -m 0600 examples/bindings.example.json ~/.config/telemetre/bindings.json
$EDITOR ~/.config/telemetre/telemetre.env
systemctl --user daemon-reload
systemctl --user enable --now telemetre-collect.timer telemetre-quota.timer
loginctl enable-linger "$USER"
```

`telemetre-quota.service` first runs `quota sample --if-due` under its own lock and timeout, then calls `send-quota` separately for `alpha-quota-agy` and `alpha-quota-codex`; each line has a `-` prefix, so a sampling or delivery failure for one provider does not block the other. When one quota database contains multiple machines/providers/accounts, `send-quota` synchronizes and attempts only samples for the current binding tuple (machine/provider/account); other samples remain pending.

Each source unit's outer timeout must exceed the sum of its inner budgets. The collect unit allows 900s for collection plus 300s for delivery (1200s total) and sets `TimeoutStartSec=25min` (1500s). The quota unit allows 300s for sampling plus 300s for each provider send (900s total) and sets `TimeoutStartSec=20min` (1200s). Keep a strict margin when editing either value.

Centre installation:

```bash
sudo cp examples/systemd/telemetre-backup.service examples/systemd/telemetre-backup.timer \
  examples/systemd/telemetre-health.service examples/systemd/telemetre-health.timer \
  examples/systemd/telemetre-quota-daily.service examples/systemd/telemetre-quota-daily.timer \
  /etc/systemd/system/
sudo install -m 0640 -o root -g telemetre examples/telemetre.center.env.example /etc/telemetre/center.env
sudo install -m 0640 -o root -g telemetre examples/quota.center.json /etc/telemetre/quota-center.json
sudoedit /etc/telemetre/center.env
sudoedit /etc/telemetre/quota-center.json
sudo systemctl daemon-reload
sudo systemctl enable --now telemetre-backup.timer telemetre-health.timer telemetre-quota-daily.timer
```

`quota-center.json` lists both the agy and Codex accounts; `telemetre-quota-daily` does not pass `--date`, so `quota daily` selects yesterday in the configured timezone and writes the summary to `quota_daily` in the centre database. To regenerate or view a particular day, pass the date explicitly:

```bash
python3 -m telemetre.quota daily \
  --config /etc/telemetre/quota-center.json \
  --db /var/lib/telemetre/center.sqlite3 \
  --date 2026-11-05 --json
```

Wrap each long-running job in:

```text
python3 -m telemetre.delivery run --lock <lock> --timeout <seconds> -- <actual command...>
```

`run` does two things: `flock` guarantees only one instance per lock; at the deadline it sends TERM/KILL to the whole process group and returns 124. When an instance already exists, a later start prints `skipped: already running` and exits 0 without queuing or collecting twice. systemd's `TimeoutStartSec` is the outer safety net; the inner `--timeout` should be shorter.

See `examples/cron/telemetre.crontab` for the cron equivalent. Cron often has no `XDG_RUNTIME_DIR`, so its locks live under `$HOME/.local/state/telemetre/`. Cron has no systemd `TimeoutStartSec`, so collection and quota sampling as well as every delivery call must use `delivery run --timeout`.

## 5. Health checks and monitoring

The centre checks expected sources instead of listing only sources that have reported:

```bash
python3 -m telemetre.delivery check \
  --center-db /var/lib/telemetre/center.sqlite3 \
  --expect-usage alpha:personal@example.test \
  --expect-quota alpha:agy:personal@example.test \
  --expect-quota alpha:codex:personal@example.test \
  --max-age-seconds 7200 \
  --format json
```

Exit codes:

- `0`: all expected sources are healthy.
- `3`: a source has never reported, a scan reports the source missing, the latest quota attempt failed, or a sample exceeds the freshness threshold.
- `2`: argument, database, binding, or quota-store configuration error.

`--expect-usage` and `--expect-quota` may be repeated. When no expected sources are listed, only usage sources already present at the centre are checked; production monitoring should list every machine/account explicitly, otherwise "never reported" will not be detected. Each JSON item includes fields such as `kind`, `machine`, `account`/`provider`, `reason`, and `last_success`.

## 6. Online backup

Each centre backup uses the SQLite online backup interface and does not disturb the running database:

```bash
python3 -m telemetre.delivery backup \
  --center-db /var/lib/telemetre/center.sqlite3 \
  --dest /var/backups/telemetre
```

The command first writes a temporary file, runs `PRAGMA integrity_check` and `PRAGMA foreign_key_check`, then atomically renames it to an independent snapshot; failure leaves no partial file. Output includes `path`, `tables`, `row_counts`, and `integrity`. Verify independently at any time:

```bash
python3 -m telemetre.delivery verify-backup --backup /var/backups/telemetre/telemetre-center-....sqlite3
```

Snapshots contain both usage and quota tables (if the centre has written them). Retention of the backup directory is handled externally, for example with filesystem snapshots or periodic archives; this project does not delete old snapshots automatically.

## 7. Restore

Restore only writes to a **new path that does not exist**; it never replaces the running database or moves its `-wal`/`-shm`. It installs with an atomic no-clobber link; of two concurrent restores, only one can succeed, and an existing target file stays unchanged.

Before restoring, quiesce the source and centre sides separately:

1. On every source host, stop that user's source timers and services with the user manager:
   ```bash
   systemctl --user stop telemetre-collect.timer telemetre-quota.timer
   systemctl --user stop telemetre-collect.service telemetre-quota.service
   ```
   This is not a `sudo systemctl` operation on the centre.
2. On the centre, stop the centre writers, including the daily quota digest:
   ```bash
   sudo systemctl stop telemetre-backup.timer telemetre-health.timer telemetre-quota-daily.timer
   sudo systemctl stop telemetre-backup.service telemetre-health.service telemetre-quota-daily.service
   ```
3. Wait for in-flight SSH deliveries to finish or terminate them. Each accepted delivery is a forced-command receiver process; check with `pgrep -af 'telemetre.delivery receive'` (or `ss`) and do not switch to the restored database until they are gone. A receiver that commits after the snapshot is exactly data that the restore does not contain.

Then restore and verify:

```bash
# 1. Restore to a brand-new path (it must not already exist)
python3 -m telemetre.delivery restore \
  --backup /var/backups/telemetre/telemetre-center-....sqlite3 \
  --center-db /var/lib/telemetre/center-restored.sqlite3

# 2. Produce the monthly report and quota history from the restored database, verify them, then switch
python3 -m telemetre report --center-db /var/lib/telemetre/center-restored.sqlite3 \
  --month 2026-11 --format json
python3 -m telemetre.quota report --db /var/lib/telemetre/center-restored.sqlite3 --json
```

After verification, change `CENTER_DB` in `/etc/telemetre/center.env` to the new path (also update `--center-db` in the server forced commands), reload, and start the timers. Keep the old database as-is for post-incident inspection or manual archiving; cleanup is an explicit operator action. If the restore target already exists, the command fails without overwriting.

Restore only returns to the latest successful snapshot. Distinguish two kinds of post-snapshot data:

- Items still pending on clients (usage `pending` rows or quota outbox rows not marked delivered) are retried automatically after the restored centre is active, and idempotency prevents double counting.
- Items the centre ACKed after the snapshot are not in the snapshot and are **not replayed automatically**: the client removed the usage pending rows or marked the quota outbox delivered only after that ACK. Retained agy source logs or the original local quota database may permit an explicit, operator-driven recovery, but this guide does not promise that every ACKed post-backup item can be reconstructed. Treat the snapshot as the recovery point.

## 8. Retry, acknowledgement, and recovery semantics

- Clients delete `pending` (usage) or mark the outbox delivered (quota) only after receiving a valid ACK. Timeout, host-key failure, non-zero exit, non-JSON ACK, missing or non-integer ACK counters, an inconsistent `stored + duplicates != accepted`, identity mismatch, and oversized output all retain the pending item.
- Centre usage receive emits its ACK only after the transaction commits; quota receive calls `ingest_observation` for one sample and ACKs only after the single-sample transaction commits. `duplicates` after a lost ACK means only a duplicate; it does not increase counts.
- Request bodies have row and byte limits; the server reads `max_bytes + 1` first and rejects the entire batch when over the limit. A bad usage line/batch does not write partial events.
- When host-key verification fails, do not disable `StrictHostKeyChecking`; verify the new fingerprint out of band and update the pinned `known_hosts`.
- While offline, the client keeps collecting into local SQLite; after centre recovery, items still pending replay in order. Once source logs are cleaned up, facts that were never staged cannot be recovered.

## 9. Security boundary

- Identity comes only from the binding associated with the SSH public key; editing the request JSON cannot impersonate another machine, account, provider, or data kind.
- Message bodies, prompts, tool arguments, environment variables, and OAuth/API credentials never enter the protocol. The receiver re-validates quota samples: it rejects inconsistent display percentages, nested/anomalous `reset.raw` shapes, unknown anomaly categories, and unsupported fields; error details are reduced to fixed safe categories, and raw provider text is not stored.
- A quota request accepts exactly one observation; failure observations carry no windows. A bad batch is rejected before any write.
- Clients pin host public keys and disable interactive authentication and forwarding; the server uses a per-key forced command, `restrict`, a dedicated user, and a hardened sshd.
- Key rotation: add the new public key to the binding and `authorized_keys`, verify, then remove the old line; removing a key revokes write permission for that binding. Binding files contain no secrets, but still restrict write access.

## 10. Troubleshooting

| Symptom | Check |
| --- | --- |
| `Host key verification failed` | Verify the centre fingerprint out of band and update `--known-hosts`; do not disable strict verification. |
| `acknowledgement ... does not match` | Does the key's forced command use the correct `--binding-id`? Does the binding table match the same machine/account/provider? |
| `ssh delivery timed out` | Centre receive or the network is too slow; the pending queue remains. Check server lock waits and backup/maintenance windows, then retry. |
| `batch is ... limit` / `request body exceeds` | Client and server `--max-bytes` differ, or one fact is too large; lower `--max-rows` and keep both limits consistent. |
| `target database already exists` | Restore must choose a new path; do not delete the live database "to make room"; stop services and keep the old database. |
| `check` exits 3 | Inspect the JSON `reason`: `no successful scan reported`, `expected source missing`, `latest attempt failed`, or `stale sample`. |
| `run ... skipped: already running` | The previous scheduled job has not finished; this is single-instance protection. Check the lock file and the corresponding service state. |

## 11. Acceptance

This slice's behavioral tests use only synthetic data and local fake SSH executables; they do not read real credentials or contact real hosts. End-to-end tests cover two clients, restricted delivery, retry after lost ACK/timeout/host-key failure, concurrent submissions, backup/restore, and expected-source health checks:

```bash
PYTHONPATH=/opt/telemetre python3 -m unittest discover -s tests -v
```

For real-host acceptance, use isolated machine/account aliases and dedicated keys, configure per sections 2 and 3, run `delivery check` first to confirm sources, then run `collect` → `send` → `report` once. Do not use real accounts for automated write tests.
