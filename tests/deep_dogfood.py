#!/usr/bin/env python3
"""
Deep dogfooding test for AXIOM's agent-task loop (PR #191).

Drives the REAL `axiom-agent-task` binary through a realistic multi-attempt
agentic scenario, exercising every protocol feature:

  1. task_start + propose(fail) + task_history + propose(different) -> history shows both
  2. Deduplication: re-propose identical failed edit -> "already rejected", verifier NOT re-run
  3. max_attempts: burn through the budget -> 3rd attempt rejected
  4. Allowlist: edit outside allowlist -> rejected
  5. Byte-for-byte rollback: file hash identical after failed propose
  6. Abort: 2 successful proposes, then finish(commit=false) -> pre-task state restored
  7. JSON-RPC notification (no "id") -> no response written

Usage:
  python3 tests/deep_dogfood.py --bin /path/to/axiom-agent-task

Exit code 0 = all tests pass, non-zero = failures (with details).
"""

import argparse
import hashlib
import json
import os
import queue
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    status = PASS if cond else FAIL
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""), flush=True)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


class TaskSession:
    """A live JSON-RPC session with the axiom-agent-task binary."""

    def __init__(self, bin_path):
        if not os.path.exists(bin_path):
            raise RuntimeError(f"binary not found: {bin_path}")
        self.proc = subprocess.Popen(
            [bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._next_id = 1
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()

    def _pump(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    self._responses.put(line)
        except Exception:
            pass

    def call(self, method, params, timeout=15):
        rid = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        try:
            raw = self._responses.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"no response for {method} (id={rid}) within {timeout}s")
        resp = json.loads(raw)
        if resp.get("id") != rid:
            raise RuntimeError(f"response id mismatch: expected {rid}, got {raw[:200]}")
        if "error" in resp:
            raise RuntimeError(f"JSON-RPC error: {resp['error']}")
        return resp.get("result")

    def notify(self, method, params):
        """Send a JSON-RPC notification (no id). Returns None."""
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def try_read_response(self, timeout=2):
        """Try to read one response line. Returns the raw line or None on timeout."""
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def make_verifier(log_path, exit_code):
    """Create a verifier script that logs each invocation and exits with exit_code."""
    script = f"""#!/bin/bash
echo "invoke $(date +%s%N)" >> {log_path}
exit {exit_code}
"""
    fd, path = tempfile.mkstemp(suffix=".sh")
    with os.fdopen(fd, "w") as f:
        f.write(script)
    os.chmod(path, 0o755)
    return path


def verifier_invocations(log_path):
    if not os.path.exists(log_path):
        return 0
    with open(log_path) as f:
        return sum(1 for line in f if line.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True, help="path to axiom-agent-task binary")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="axiom-deep-")
    print(f"workdir: {workdir}", flush=True)

    try:
        # ---- shared fixtures ----
        target = os.path.join(workdir, "target.txt")
        other = os.path.join(workdir, "other.txt")
        outside = os.path.join(workdir, "outside.txt")
        with open(target, "w") as f:
            f.write("v1-original\n")
        with open(other, "w") as f:
            f.write("other-original\n")
        with open(outside, "w") as f:
            f.write("outside-original\n")

        # ============================================================
        # TEST 1+2: propose -> fail -> history -> propose different -> history
        # ============================================================
        log1 = os.path.join(workdir, "verifier1.log")
        verifier_fail = make_verifier(log1, 1)
        s = TaskSession(args.bin)

        r = s.call("task_start", {
            "goal": "deep dogfood multi-attempt",
            "verify_cmd": verifier_fail,
            "files": [target],
            "max_attempts": 5,
        })
        task_id = r["task_id"]
        check("T1 task_start returns task_id", bool(task_id), f"got {r}")

        pre_hash = sha256_file(target)

        # Attempt 1: fails the verifier
        o1 = s.call("task_propose", {
            "task_id": task_id,
            "edits": [{"path": target, "content": "v2-bad-attempt\n"}],
        })
        check("T1 attempt1 rejected (verifier failed)", not o1["passed"],
              f"passed={o1.get('passed')} output={str(o1.get('output'))[:120]}")
        check("T1 verifier ran once", verifier_invocations(log1) == 1,
              f"invocations={verifier_invocations(log1)}")

        # Agent reads history before retrying (the agentic loop)
        h1 = s.call("task_history", {"task_id": task_id})
        attempts = h1["attempts"] if isinstance(h1, dict) else h1
        check("T2 history shows 1 attempt", len(attempts) == 1,
              f"len={len(attempts)}")
        check("T2 history attempt1 recorded as failed",
              attempts and not attempts[0].get("passed", True),
              f"{str(attempts[0])[:150]}")

        # Attempt 2: DIFFERENT edit. Swap verifier to one that passes.
        log2 = os.path.join(workdir, "verifier2.log")
        # NOTE: verify_cmd is fixed at task_start; we cannot change it.
        # Instead we start a new task for the passing case below (T6/T7).
        # Here attempt 2 also fails but with different content.
        o2 = s.call("task_propose", {
            "task_id": task_id,
            "edits": [{"path": target, "content": "v3-different-bad\n"}],
        })
        check("T2 attempt2 (different edit) also rejected", not o2["passed"])
        check("T2 verifier ran twice total", verifier_invocations(log1) == 2,
              f"invocations={verifier_invocations(log1)}")

        h2 = s.call("task_history", {"task_id": task_id})
        attempts2 = h2["attempts"] if isinstance(h2, dict) else h2
        check("T2 history shows 2 attempts", len(attempts2) == 2,
              f"len={len(attempts2)}")
        fps = [a.get("fingerprint") for a in attempts2]
        check("T2 the two attempts have different fingerprints", len(set(fps)) == 2,
              f"fps={fps}")

        # ============================================================
        # TEST 3: deduplication - identical failed edit NOT re-run
        # ============================================================
        inv_before = verifier_invocations(log1)
        o3 = s.call("task_propose", {
            "task_id": task_id,
            "edits": [{"path": target, "content": "v2-bad-attempt\n"}],  # identical to attempt 1
        })
        check("T3 duplicate rejected", not o3["passed"])
        check("T3 duplicate says 'already rejected'",
              "already rejected" in str(o3.get("output", "")),
              f"output={str(o3.get('output'))[:150]}")
        check("T3 verifier NOT re-run for duplicate",
              verifier_invocations(log1) == inv_before,
              f"before={inv_before} after={verifier_invocations(log1)}")
        check("T3 duplicate has same fingerprint as attempt1",
              o3.get("fingerprint") == attempts2[0].get("fingerprint"),
              f"{o3.get('fingerprint')} vs {attempts2[0].get('fingerprint')}")

        # ============================================================
        # TEST 5: allowlist - edit outside allowlist rejected
        # ============================================================
        o5 = s.call("task_propose", {
            "task_id": task_id,
            "edits": [{"path": outside, "content": "hacked\n"}],
        })
        check("T5 outside-allowlist edit rejected", not o5["passed"])
        check("T5 rejection mentions allowlist",
              "allowlist" in str(o5.get("output", "")).lower(),
              f"output={str(o5.get('output'))[:150]}")
        check("T5 outside file untouched",
              open(outside).read() == "outside-original\n")
        check("T5 verifier NOT run for allowlist rejection",
              verifier_invocations(log1) == inv_before,
              f"invocations={verifier_invocations(log1)}")

        # ============================================================
        # TEST 6: byte-for-byte rollback after failed propose
        # ============================================================
        post_hash = sha256_file(target)
        check("T6 file byte-identical after failed proposes", pre_hash == post_hash,
              f"pre={pre_hash[:12]} post={post_hash[:12]}")
        check("T6 file content still original", open(target).read() == "v1-original\n")

        s.call("task_finish", {"task_id": task_id, "commit": True})
        s.close()

        # ============================================================
        # TEST 4: max_attempts enforcement
        # ============================================================
        log4 = os.path.join(workdir, "verifier4.log")
        s4 = TaskSession(args.bin)
        r4 = s4.call("task_start", {
            "goal": "max attempts test",
            "verify_cmd": make_verifier(log4, 1),  # always fails
            "files": [target],
            "max_attempts": 2,
        })
        t4 = r4["task_id"]
        # Use DISTINCT contents so dedup doesn't kick in
        a1 = s4.call("task_propose", {"task_id": t4,
            "edits": [{"path": target, "content": "attempt-one\n"}]})
        a2 = s4.call("task_propose", {"task_id": t4,
            "edits": [{"path": target, "content": "attempt-two\n"}]})
        check("T4 first two attempts consumed (not max-attempts rejections)",
              "max attempts" not in str(a1.get("output", "")).lower()
              and "max attempts" not in str(a2.get("output", "")).lower(),
              f"a1={str(a1.get('output'))[:80]} a2={str(a2.get('output'))[:80]}")
        a3 = s4.call("task_propose", {"task_id": t4,
            "edits": [{"path": target, "content": "attempt-three\n"}]})
        check("T4 third attempt rejected", not a3["passed"])
        check("T4 rejection says 'max attempts'",
              "max attempts" in str(a3.get("output", "")).lower(),
              f"output={str(a3.get('output'))[:150]}")
        check("T4 verifier ran exactly twice (not for rejected 3rd)",
              verifier_invocations(log4) == 2,
              f"invocations={verifier_invocations(log4)}")
        s4.call("task_finish", {"task_id": t4, "commit": True})
        s4.close()

        # ============================================================
        # TEST 7: abort restores PRE-TASK state (not last-committed)
        # ============================================================
        log7 = os.path.join(workdir, "verifier7.log")
        s7 = TaskSession(args.bin)
        with open(target, "w") as f:
            f.write("v1-original\n")
        with open(other, "w") as f:
            f.write("other-original\n")
        r7 = s7.call("task_start", {
            "goal": "abort semantics test",
            "verify_cmd": make_verifier(log7, 0),  # always passes
            "files": [target, other],
            "max_attempts": 5,
        })
        t7 = r7["task_id"]
        p1 = s7.call("task_propose", {"task_id": t7,
            "edits": [{"path": target, "content": "v2-committed\n"}]})
        check("T7 first propose passed", p1["passed"], f"output={str(p1.get('output'))[:120]}")
        p2 = s7.call("task_propose", {"task_id": t7,
            "edits": [{"path": other, "content": "other-v2\n"}]})
        check("T7 second propose passed", p2["passed"])
        check("T7 files show committed state",
              open(target).read() == "v2-committed\n"
              and open(other).read() == "other-v2\n")
        f7 = s7.call("task_finish", {"task_id": t7, "commit": False})
        check("T7 finish(commit=false) acknowledged", True, f"{f7}")
        check("T7 target restored to PRE-TASK state (not v2)",
              open(target).read() == "v1-original\n",
              f"got {open(target).read()!r}")
        check("T7 other restored to PRE-TASK state",
              open(other).read() == "other-original\n",
              f"got {open(other).read()!r}")
        s7.close()

        # ============================================================
        # TEST 8: JSON-RPC notification (no id) -> NO response
        # ============================================================
        s8 = TaskSession(args.bin)
        # Drain anything pending
        time.sleep(0.3)
        while s8.try_read_response(timeout=0.1) is not None:
            pass
        s8.notify("task_history", {"task_id": "nonexistent"})
        got = s8.try_read_response(timeout=2)
        check("T8 no response written for notification", got is None,
              f"got unexpected response: {str(got)[:150]}")
        # Server must still be alive: a normal call afterwards works
        r8 = s8.call("task_start", {
            "goal": "post-notification liveness",
            "verify_cmd": "exit 0",
            "files": [target],
            "max_attempts": 1,
        })
        check("T8 server alive after notification", bool(r8.get("task_id")))
        s8.call("task_finish", {"task_id": r8["task_id"], "commit": True})
        s8.close()

        # ============================================================
        # TEST 9: unknown task_id -> clean error (no panic)
        # ============================================================
        s9 = TaskSession(args.bin)
        try:
            s9.call("task_propose", {"task_id": "does-not-exist",
                "edits": [{"path": target, "content": "x\n"}]}, timeout=10)
            # If the binary returns a JSON-RPC error, call() raises. If it
            # returns a normal result with passed=false, that's also fine.
            check("T9 unknown task_id handled without hang", True)
        except RuntimeError as e:
            check("T9 unknown task_id -> JSON-RPC error (no panic/hang)", "error" in str(e).lower() or "unknown" in str(e).lower(), str(e)[:120])
        # liveness
        r9 = s9.call("task_start", {"goal": "x", "verify_cmd": "exit 0",
                                    "files": [target], "max_attempts": 1})
        check("T9 server alive after unknown task_id", bool(r9.get("task_id")))
        s9.close()

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n================ SUMMARY ================", flush=True)
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} -- {detail}")
    print(f"\n{passed}/{total} checks passed", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
