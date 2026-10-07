# axiom-task-deep-test

Deep dogfooding harness for AXIOM's agent-task loop
([PR #191](https://github.com/fernandogarzaaa/AXIOM-AETHER/pull/191),
merged as `87ea1f3`).

The shallow test (`axiom-rag`) only ever did a single `task_propose` per
task. This harness drives the **real** `axiom-agent-task` binary through a
realistic multi-attempt agentic scenario, exercising every protocol feature.

## What each test proves

| ID | Test | What it proves |
|----|------|----------------|
| T1 | `task_start` + failing `task_propose` | The verifier gate works: a failing verifier rejects the proposal and the file is untouched |
| T2 | `task_history` after 2 attempts | History records every attempt in order, with distinct fingerprints per unique edit-set |
| T3 | Re-propose identical failed edit | **Deduplication**: rejected with "already rejected" and the verifier is NOT re-executed (invocation count unchanged) |
| T4 | `max_attempts=2`, 3 proposes | The attempt budget is enforced; the 3rd attempt is rejected without running the verifier |
| T5 | Edit outside `files` allowlist | Path enforcement: rejected before apply, file untouched, verifier not run |
| T6 | SHA-256 before/after failed proposes | **Byte-for-byte rollback**: failed proposals leave zero trace on disk |
| T7 | 2 passing proposes, then `finish(commit=false)` | **Abort semantics**: files restored to the pre-task snapshot, not the last-committed state |
| T8 | JSON-RPC request without `"id"` | **Notification handling**: no response is written; the server stays alive for subsequent requests |
| T9 | `task_propose` with unknown `task_id` | Clean JSON-RPC error, no panic, no hang; server stays alive |

## Key design choices in the harness

- **Verifier invocation counting**: each `verify_cmd` is a shell script that
  appends to a log file before exiting. This lets T3/T4/T5 prove the verifier
  was *not* re-run (not just that the output looked right).
- **Distinct edit contents** in T4: the dedup check runs before the
  max-attempts check, so reusing the same content would mask the budget test.
- **Notification liveness check** in T8: after asserting no response, a normal
  `task_start` proves the server didn't die silently.

## Running

```bash
# Build the binary from AXIOM-AETHER main
cd ~/workspace/audits/axiom-aether
git fetch origin main && git reset --hard origin/main
cd axiom_engine_rs
cargo build --release --locked --bin axiom-agent-task

# Run the harness
python3 tests/deep_dogfood.py --bin axiom_engine_rs/target/release/axiom-agent-task
```

Exit code 0 means all checks passed. Any failure prints the check name and
the observed-vs-expected detail.

## Requirements

- Python 3.8+ (stdlib only: no third-party packages)
- Rust toolchain (to build the binary under test)
- No API keys, no network access needed. Fully offline.
