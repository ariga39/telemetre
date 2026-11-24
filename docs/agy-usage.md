# telemetre (agy usage slice)

First end-to-end vertical slice for issue #1: scan local Antigravity CLI (`agy`)
transcripts, spool offline in SQLite, deliver bounded batches to a centre SQLite
database, and print a cross-machine monthly token report.

This slice is Python 3 standard library only. It does **not** open SSH
connections; it exposes the local receive/replay seam that slice #3 will invoke
over SSH.

## Requirements

- Python 3.11+ (`zoneinfo` is used for `--tz`)
- SQLite (bundled with Python)

No installation or third-party packages are required.

## Quick start (synthetic fixture)

From the repository root:

```bash
tmp=$(mktemp -d /tmp/telemetre-demo.XXXXXX)
mkdir -p "$tmp/root/brain/conv-1/.system_generated/logs"
cat > "$tmp/root/brain/conv-1/.system_generated/logs/transcript.jsonl" <<'JSONL'
{"step_index":0,"created_at":"2026-11-01T00:00:00Z","type":"PLANNER_RESPONSE","status":"DONE","model":"gemini-3-pro","input_tokens":100,"cache_read_tokens":20,"output_tokens":5}
{"step_index":1,"created_at":"2026-11-02T00:00:00Z","type":"PLANNER_RESPONSE","status":"DONE","model":"gemini-3-pro","input_tokens":0,"cache_read_tokens":0,"output_tokens":0}
JSONL

python3 -m telemetre collect \
  --client-db "$tmp/client.sqlite" \
  --machine alpha --account acct@example.test \
  --root "$tmp/root"

python3 -m telemetre replay \
  --client-db "$tmp/client.sqlite" \
  --center-db "$tmp/center.sqlite" \
  --machine alpha --account acct@example.test

python3 -m telemetre report \
  --center-db "$tmp/center.sqlite" \
  --month 2026-11 --tz UTC

python3 -m telemetre status \
  --center-db "$tmp/center.sqlite" \
  --expected alpha:acct@example.test
```

Every command accepts `--help`.

## Commands

### `collect`

```bash
python3 -m telemetre collect \
  --client-db PATH --machine ALIAS --account ALIAS \
  --root PATH [--root PATH ...] [--format text|json]
```

Scans each root for:

```text
brain/<conversation-id>/.system_generated/logs/transcript.jsonl
```

A model event requires the sampled agy shape `type=PLANNER_RESPONSE` and
`status=DONE`, plus `step_index`, `created_at`, `model`, and the three optional
counts. Tool/user rows are ignored. Complete newline-terminated JSON rows are
parsed; a trailing partial row waits for the next run. Malformed middle rows are
counted in diagnostics and later rows continue.

The local transaction commits file signatures, fact digests and pending rows
together. Unchanged files (device, inode, size, nanosecond mtime) are skipped. A
file that changes during a read retry does not advance its stored signature.

### `pending`

```bash
python3 -m telemetre pending --client-db PATH [--max-rows N] [--max-bytes N]
```

Prints the next bounded wire batch as JSON. It does not delete anything. Useful
for an SSH wrapper that needs to inspect or deliver a batch itself.

### `replay`

```bash
python3 -m telemetre replay \
  --client-db PATH --center-db PATH \
  --machine ALIAS --account ALIAS [--max-rows N] [--max-bytes N]
```

Sends bounded batches through the local receive seam and deletes spool rows only
after a matching acknowledgement. The machine/account flags must match the
identity already bound in the client spool.

A pending row survives a lost response, malformed/negative ACK, a wrong ACK
identity/kind, or a 0-of-N ACK. A single fact that cannot fit the byte cap fails
clearly and remains in the spool.

### `receive` (local seam for #3)

```bash
python3 -m telemetre receive \
  --center-db PATH --machine ALIAS --account ALIAS [--kind agy_usage] \
  [--max-rows N] [--max-bytes N] < batch.json
```

Reads one bounded JSON batch from stdin. The caller supplies the authenticated
machine/account/kind context. Payload claims of machine/account are ignored.
Validation covers protocol version, kind, row/byte limits, timestamp
parseability, field types and non-negative integer counts. The transaction
commits before an ACK is printed. A rejected batch is not partially stored.

`receive` prints a JSON ACK containing `ok`, `accepted`, `stored`, `duplicates`,
`conflicts`, `kind`, `machine` and `account`. Protocol errors print only
`{"ok": false, "error": "invalid batch"}`; raw exception text and local
filesystem roots are not returned.

#3 must derive `machine`, `account` and `kind` from the SSH key binding, not
from the remote payload, then call this seam.

### `report`

```bash
python3 -m telemetre report \
  --center-db PATH --month YYYY-MM [--tz ZONE] [--format text|json]
```

- Calendar month is selected in `--tz` (default `UTC`), then converted to a
  left-closed, right-open UTC range.
- Events are assigned by transcript `created_at`, never by collection/upload
  time.
- Three columns are summed independently: `input_tokens`,
  `cache_read_tokens`, `output_tokens`. No cross-column total is printed.
- Missing and explicit zero are distinct: sums include known values, while
  coverage reports the number of model steps with each field missing.
- Per-machine/model rows and per-account totals are shown. A repeated event seen
  on multiple machines is counted once in the account total but removed from
  single-machine ranking and shown as `unattributed` with its source set.
- Same-key events with conflicting counts are excluded from known totals and
  reported with every structured observation.
- Source status includes last successful scan, bad lines, missing-count steps,
  pending backlog, read errors (`source_errors`) and missing roots.
- Reasoning coverage is always `unknown`: no reasoning value is collected or
  inferred from message text.
- Source version is `unknown`; only the collector version is recorded.

### `status`

```bash
python3 -m telemetre status \
  --center-db PATH --expected MACHINE:ACCOUNT [--max-age-seconds N] \
  [--format text|json]
```

Exits non-zero when an expected source has never reported, its last scan is
stale, or its configured root was missing. Without `--expected`, all sources
known to the centre are checked.

## Wire protocol

Batch object:

```json
{
  "protocol": 1,
  "kind": "agy_usage",
  "collector_version": "0.1.0",
  "scan": {
    "run_id": "…",
    "completed_at": "2026-11-24T10:00:00Z",
    "files_seen": 1,
    "files_read": 1,
    "files_skipped": 0,
    "partial_files": 0,
    "bad_lines": 0,
    "missing_count_steps": 0,
    "new_facts": 1,
    "changed_facts": 0,
    "pending_backlog": 1,
    "source_missing": 0,
    "source_errors": 0
  },
  "facts": [
    {
      "conversation_id": "conv-1",
      "step_index": 0,
      "event_time": "2026-11-01T00:00:00Z",
      "model": "gemini-3-pro",
      "input_tokens": 100,
      "cache_read_tokens": 20,
      "output_tokens": 5
    }
  ]
}
```

Local filesystem roots, message text, tool arguments and credentials are not in
the batch. A count is either a non-negative integer or `null` (missing). The
receiver treats missing as missing, never as zero.

Centre identity is `(account, conversation_id, step_index)`. Idempotency is per
content digest plus source machine. A replay of the same content stores no new
observation. A different count for the same identity is conflict evidence, not
an overwrite.

## Fork/rewind scope (unverified)

The implementation does **not** claim exact general fork/rewind semantics.
Observed evidence from agy 1.5.16 disposable probes:

- Forking a parent with one metered step first produced a child conversation DB
  with a `parent_reference` and no transcript. After one new child call, the
  child transcript held one metered row and zero matching parent meter tuples.
  This suggests that probe did not copy the metered prefix, but it is not proof
  for every fork path.
- One `/rewind` plus follow-up probe left the parent transcript with original
  step 1 and later step 4. This does not establish general rewind behaviour.

Conservative handling used here:

- An identical copied step on another machine is deduplicated and marked
  attribution-ambiguous.
- A reused `(conversation_id, step_index)` with different counts keeps all
  observations and isolates the event as a conflict.
- No parent-reference-based exclusion is implemented. Where a fork/rewind
  copies or reuses indices differently from these probes, coverage is uncertain
  and totals must not be described as exact.



## Tests

```bash
python3 -m unittest discover -s tests -v
```

All fixtures are synthetic. Tests never read a real agy data root. They cover
CLI boundaries, two roots, two machines, UTC/`Asia/Tokyo` month selection,
partial lines, bad lines, explicit zero versus missing, unchanged-file skip,
deleted sources, lost ACKs, conflicts, ambiguous attribution, causal merges,
receive bounds, identity binding, out-of-order scan status, read errors and
unstable reads.

## Interface stability for slice #3

The following are the intended stable seams in this snapshot:

- CLI: `collect`, `pending`, `replay`, `receive`, `report`, `status`.
- Python receive: `telemetre.receiver.receive(center_db, IngestContext,
  raw_bytes, max_rows=…, max_bytes=…)`.
- Python replay: `telemetre.replay.replay_pending(client_db, transport,
  max_rows=…, max_bytes=…)` with any object exposing `send(bytes) -> ack dict`.
- Wire protocol version `1`, kind `agy_usage`, and the ACK fields listed above.

Any change to these names, ACK fields or batch shape after this snapshot should
be treated as a coordinated interface change with slice #3.
