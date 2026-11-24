# Quota sampler (slice 2)

`telemetre.quota` is the standalone quota vertical slice. It samples the two
subscription sources through their real local protocols, stores sanitized
snapshots in SQLite, and prints current or dated reports. It does **not** send
anything to a central host; the small export/receive seam for slice 3 is
described at the end.

The module uses only the Python standard library. It does not read chat
transcripts, prompts, tool arguments, environment dumps, or credentials, and it
does not make provider network requests. Tests use synthetic executable
fixtures in temporary directories.

## Requirements and files

- Python 3.11 or newer.
- `telemetre/quota.py` — the complete sampler and CLI.
- `tests/test_quota*.py` — behavioral tests, including synthetic `agy` and
  `codex app-server` peers.
- This document.

The package is a namespace package; no `telemetre/__init__.py` is required.

## Configuration

The default path is `~/.config/telemetre/quota.json`, overridable with
`--config` or `TELEMETRE_QUOTA_CONFIG`. The default database is
`~/.local/state/telemetre/quota.sqlite3`, overridable with `--db`,
`TELEMETRE_QUOTA_DB`, or `database` in the configuration.

```json
{
  "machine": "studio",
  "database": "/var/lib/telemetre/quota.sqlite3",
  "timezone": "Asia/Shanghai",
  "providers": [
    {
      "provider": "agy",
      "account": "personal",
      "interval_seconds": 900,
      "timeout_seconds": 60
    },
    {
      "provider": "codex",
      "account": "work",
      "interval_seconds": 900,
      "timeout_seconds": 60
    }
  ]
}
```

`machine` is the stable alias of the sampling host and is copied into every
sanitized observation. An account is identified by `(provider, account)`; the
same account observed from two machines is never summed.

If `command` is omitted, the defaults are:

- `agy`: `["agy", "-p", "/usage", "--output-format", "json"]`
- `codex`: `["codex", "app-server"]`

A `command` override is mainly useful for a wrapper, an isolated CLI profile,
or a synthetic test. Keep credentials in the provider's own login store, not
in this file.

`interval_seconds` is per source. Start at `900` (15 minutes). A real check of
agy 1.5.16 `/usage` took about 6 seconds, added roughly 15 KB of CLI logs, and
created no new session files or model turns. A 5-minute interval should only be
chosen after the slice-2 scheduling check on the target installation; the
sampler itself does not force a particular frequency.

## Commands

```sh
python3 -m telemetre.quota sample --config ~/.config/telemetre/quota.json
python3 -m telemetre.quota report
python3 -m telemetre.quota report --date 2026-01-01 --tz Asia/Shanghai
python3 -m telemetre.quota daily --date 2026-01-01
```

All commands support `--config`, `--db`, `--tz`, `--now` (RFC3339, useful for
tests and replay checks), and `--json`.

- `sample` reads every configured provider, stores one sample per provider
  attempt, and exits non-zero if any attempt failed. It still samples the other
  provider when one fails.
- `sample --if-due` skips a source whose last attempt for this machine is
  younger than its configured interval. This is the activity-independent
  scheduling hook: run it from a system timer every minute (or every five
  minutes) instead of tying collection to a model call.
- `report` prints the latest successful snapshot per account, plus the latest
  attempt status. With `--date`, it prints observations from that local
  calendar day. `report --check` exits 3 when an **expected** account is
  missing, stale, or its latest attempt failed. When a config is supplied, only
  accounts still configured are expected; accounts retained in the database
  after removal stay visible as history but do not make the check fail.
- `daily` builds the same dated summary and stores it by `(day, timezone)`.
  Re-running for the same day updates the one stored row. Without `--date`, it
  targets yesterday in the report timezone.

Example systemd timer unit:

```ini
[Unit]
Description=Sample subscription quota

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -m telemetre.quota sample --if-due
WorkingDirectory=/opt/telemetre
```

Use one process per repository checkout, or set `PYTHONPATH` explicitly. The
command exits after sampling; it never stays resident and never interrupts a
provider session.

## agy protocol

`telemetre.quota` runs the configured command and requires the JSON envelope to
have an explicit successful status (`"success"` or `"ok"`). A missing or
non-success status is a failed observation, not an empty or zero reading. It
then reads:

```text
command.data.groups[].buckets[]
```

Each bucket contributes `group.name`, `bucket.id`, `window`,
`remaining_fraction`, and `reset_time`. `remaining_fraction` is kept as the
source decimal and displayed as `100 × fraction`; display percentages derived
from a separate UI percentage field are not used. Values outside `[0, 1]` are
kept and flagged, and the displayed remaining value is floored at `0%`.
`reset_time` is parsed as RFC3339 and stored in UTC; its original value is also
kept. Missing, zero, or unparseable reset values are shown as unknown, with a
parse problem flagged.

An empty `groups` list is a successful observation with `empty_windows: true`
(“no windows returned”), not a failure.

## Codex app-server protocol

The default command starts `codex app-server` on stdio. The sampler:

1. sends `initialize` and waits for the matching response id;
2. sends the `initialized` notification;
3. sends `account/rateLimits/read` and waits for the matching response id.

Notifications or unrelated ids on the stream are ignored. The child is placed
in its own process group and is terminated on success, error, and timeout; a
timed-out provider and its descendants are sent `SIGTERM`, then `SIGKILL` after
a short grace period.

The response view is chosen as follows:

- use `rateLimitsByLimitId` when it is a non-empty mapping;
- otherwise use the valid `rateLimits` object;
- never merge both views.

For each limit the report keeps `limitId` and the `primary`/`secondary` slots.
For each slot it keeps `windowDurationMins`, `usedPercent`, and `resetsAt`.
Durations are interpreted as minutes (`10080` is seven days). A negative
`windowDurationMins` is a malformed source response and is recorded as a failed
observation; it does not replace the last complete snapshot. `usedPercent` is
kept as the source value and displayed as `100 − usedPercent`, floored at `0%`
for overage; out-of-range values are flagged. `resetsAt` is Unix seconds,
converted to UTC, and the original value is retained. Missing optional fields
are marked unknown rather than treated as zero. A null slot is omitted; an
empty recognized result is a successful observation with no windows.

## Failure semantics

A timeout, spawn error, non-zero exit, re-login/authentication message,
non-JSON output, unrecognized envelope, malformed bucket/slot, non-finite
number, or a finite source number whose derived display overflows (for example
`remaining_fraction: 1e308`) is a **failure observation**:

- it is stored with a stable sample id and a short generic error kind/detail;
- it carries no partial buckets;
- it does not replace the latest successful snapshot;
- it never becomes `0%` or `100%`.

The latest attempt and the last success are reported separately, so a failing
source still shows its last complete reading. The last complete response is
accepted only after the whole response parses; a valid bucket followed by a
broken bucket fails the whole sample.

## Storage, replay, and ordering

SQLite stores one row per attempt and one row per bucket. `sample_id` is stable
for an observation and is reused by a retry/replay. Re-ingesting the same id
and content is a no-op; reusing the id with different content raises
`ObservationConflict`. The receiver records `received_at`, while reports order
observations by the sampler's `sampled_at`. An offline replay therefore cannot
overwrite a newer success or failure state merely because it arrived later.

Samples more than five minutes ahead of their receive time are flagged as time
anomalies; reports also show the source sample time so clock problems are
visible.

## Report semantics

Current report:

- one row per account/service/window from the latest **successful** snapshot;
- source value and unit, display percentage, reset time, sample time, machine,
  and freshness;
- latest attempt status, including failure kind, plus the last success time;
- stale after two configured sampling intervals, keeping the last reading and
  time visible;
- `REFRESH PENDING` when the reset time has passed but no later sample confirms
  the new window;
- net change only when the bucket, window duration, and reset time match, the
  previous observation exists, neither remaining value is unknown, and the
  sampling gap is at most two intervals. A refresh, identity change, or gap
  breaks comparison instead of guessing;
- an account observed on several machines shows the latest observation, not a
  sum.

Dated report:

- the local calendar day is converted to a left-closed, right-open UTC range
  (`Asia/Shanghai` on 2026-01-01 is `2025-12-31T16:00Z` through
  `2026-01-01T16:00Z`);
- only stored snapshots in the range are replayed; nothing is fetched from a
  provider;
- each window reports the minimum observed remaining percentage and its
  observation time, the latest value/time, observation count, and whether the
  range contains a sampling gap;
- consecutive zero observations in the same window count as one observed
  bottom segment. A gap does not create an inferred extra bottom, and no
  balance or bottom count is invented across a gap;
- reset changes create separate window histories;
- missing intervals are reported explicitly, including a partial current day
  ending at report time;
- dated freshness is evaluated at the requested period end (or report time for
  a partial current day), not at the wall clock when the report is viewed, so
  yesterday's complete summary can remain fresh;
- reports are per account; concurrent machine deductions are not split.

`daily` stores the dated summary with the latest value, daily minimum, last
success time, and missing intervals for each window/account, then prints it.
`report --date` recomputes the same view from stored samples.

## Slice-3 seam

The delivery/receiver slice should use this public, sanitized interface rather
than SQL:

```python
from telemetre.quota import (
    Observation,
    ingest_observation,
    iter_observations,
    read_observation,
)

for observation in iter_observations(
    local_db,
    provider=None,        # optional exact filter
    account=None,
    machine=None,
    sampled_after=None,   # RFC3339 or datetime; strictly after
):
    payload = observation.to_dict()   # schema telemetre.quota.observation.v1
    # send payload over the authenticated delivery channel

# Observation.from_dict(payload) validates the shape and raises ValueError for
# non-list windows or invalid window rows, rather than leaking AttributeError.

is_new = ingest_observation(center_db, payload, received_at=None)
# False means an idempotent replay. Raises ObservationConflict if the same
# sample_id arrived with different content.
```

`iter_observations` returns both successes and failures, ordered by sample time
and then sample id. Observations carry `provider`, `account`, `machine`,
`sampled_at`, `interval_seconds`, `status`, an optional short `error`, and the
sanitized bucket list. They never contain provider stdout/stderr, prompts,
transcripts, environment variables, or credentials.

Acknowledgement state belongs to the delivery layer; quota never deletes
samples and has no ack table. The receiver must still enforce its own binding
of SSH identity to provider/account/machine and then call
`ingest_observation`. The SQLite tables `quota_samples`/`quota_windows` are
private to this module.

## Tests

Run only the quota tests:

```sh
python3 -m unittest discover -s tests -p 'test_quota*.py'
```

Run the same tests from the repository root as an importable module:

```sh
python3 -m unittest tests.test_quota_cli
```

They can coexist with the usage-slice tests in the same `tests/` directory.
The fixtures create temporary fake `agy` and Codex stdio peers; no real account,
network request, provider log, or personal data is used.

## Known limits

- Historical agy transcript logs are a usage-slice concern; quota history comes
  only from saved snapshots.
- A local clock problem can only be detected when it is visible relative to the
  receive time.
- A failure is not converted into a reading, and a missing optional field stays
  unknown.
- Sampling does not attempt to split concurrent account consumption between
  machines.
- The central receiver, SSH transport, and daily backup are slice 3 work, not
  part of this module.
