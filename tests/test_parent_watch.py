# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""``gvanalysis.parentwatch`` — pool workers must die with their parent.

The failure this guards against leaves no trace in any output: the analysis
that was running is simply gone, and what remains is a set of processes with
PID 1 for a parent, holding their analyzers and weights, doing nothing, until
the machine reboots. Thirty-seven of them were found on the analysis Mac in
September 2026 across three separate incidents.

So the test has to actually kill a parent, hard, and count what is left --
there is no lighter proxy for it. `SIGKILL` specifically, because every softer
death already worked: an exception, a `sys.exit` and a normal fall-off-the-end
all run `ProcessPoolExecutor.__exit__`, which shuts the workers down properly.
The bug is only reachable when the parent gets no chance to run any code at
all, which is exactly what a force-quit or a `launchctl kill` does.

The control case runs first and must FAIL to clean up. That matters as much as
the guarded case passing: without it, a test that stopped exercising the bug --
a future Python that closes the queue differently, a pool that gains its own
watchdog -- would go on passing while proving nothing, and the guard could be
deleted with the suite still green.

No engine required: the unit under test is the watcher, so the pool workers
here sleep rather than analyse.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gvanalysis.parentwatch import watch_parent  # noqa: E402

_checks = 0
_failures = 0


def check(cond, label):
    global _checks, _failures
    _checks += 1
    if cond:
        print(f"OK    {label}")
    else:
        _failures += 1
        print(f"FAIL  {label}")


# The child program. Written to a real file rather than passed with `-c`
# because `spawn` re-imports the parent's `__main__` in every worker, and a
# `-c` program has no file for it to import.
CHILD = '''
import os, signal, sys, time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, __REPO__)
from gvanalysis.parentwatch import watch_parent

WORKERS = 3


def init():
    # Announce this worker before anything else, so the parent's killer knows
    # exactly which processes to hold responsible. Opened per worker in append
    # mode and closed at once: three processes share this path.
    with open(os.environ["PIDFILE"], "a") as fh:
        print(os.getpid(), file=fh)
    if os.environ.get("GUARD") == "1":
        watch_parent()


def work(n):
    time.sleep(600)       # long enough that nothing exits on its own
    return n


def main():
    with ProcessPoolExecutor(max_workers=WORKERS, initializer=init) as pool:
        for i in range(WORKERS):
            pool.submit(work, i)
        # Wait for the workers to announce themselves rather than sleeping a
        # guessed interval: killing the parent before a worker has run its
        # initializer would test nothing and would look like a pass.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if len(open(os.environ["PIDFILE"]).read().split()) >= WORKERS:
                break
            time.sleep(0.05)
        else:
            raise SystemExit("workers never started")
        time.sleep(0.5)   # let them settle into call_queue.get()
        os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    main()
'''


def still_running(pids):
    """Which of `pids` are still alive.

    Signal 0 rather than a `ps` scan, because a machine-wide scan for
    `spawn_main` is not specific enough to be trusted here: this repo's own
    analysis service runs on the same machine and its pool workers come and go
    while the test runs, which reads as leakage that is not ours. The workers
    name themselves instead, so the count is exact.
    """
    alive = []
    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            continue           # exited, which is the outcome under test
        alive.append(pid)
    return alive


def survivors(guarded: bool) -> int:
    """Run the child, kill it, and count the pool workers it left behind."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "orphan_parent.py"
        script.write_text(CHILD.replace("__REPO__", repr(str(REPO))))

        pidfile = Path(tmp) / "workers.pids"
        pidfile.touch()
        env = {
            **os.environ,
            "GUARD": "1" if guarded else "0",
            "PIDFILE": str(pidfile),
        }
        # DEVNULL, not a pipe, and this is the same mechanism as the bug: a
        # captured stdout is a pipe the workers inherit, so in the control case
        # the orphans hold its write end open and `run()` blocks forever
        # waiting on EOF from processes that have outlived their parent. Giving
        # them a file descriptor with no reader is what keeps the control case
        # from hanging the suite.
        subprocess.run(
            [sys.executable, str(script)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        workers = [int(line) for line in pidfile.read_text().split()]
        if len(workers) != 3:
            raise AssertionError(
                f"expected 3 workers to start, {len(workers)} reported in"
            )

        # Poll rather than sleep a fixed interval. The guarded case usually
        # settles in well under a second, so polling keeps this test from
        # dominating `run_all.py --fast`; the control case waits the whole
        # budget, which is correct -- proving a leak means proving the
        # processes are still there after a generous chance to leave.
        deadline = time.monotonic() + 6
        while True:
            alive = still_running(workers)
            if not alive or time.monotonic() > deadline:
                break
            time.sleep(0.1)

        for pid in alive:          # never leave the machine dirtier than found
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        return len(alive)


if os.name != "posix":
    # `SIGKILL` has no Windows equivalent, and `TerminateProcess` via SIGTERM
    # is a different death with a different cleanup path. The guard itself is
    # cross-platform (it waits on a handle there); this *reproduction* is not.
    print("SKIP  parent-death reproduction needs POSIX signals")
    sys.exit(0)

print("Running two 8-second subprocesses; the first is meant to leak.")
print()

check(watch_parent() is False,
      "watch_parent() is a no-op in a process that is nobody's pool worker")

leaked = survivors(guarded=False)
check(leaked > 0,
      f"CONTROL: an unguarded pool leaks its workers when the parent is killed "
      f"(leaked {leaked})")

kept = survivors(guarded=True)
check(kept == 0,
      f"a guarded pool leaves nothing behind (leaked {kept})")

# The guard is only worth anything if the analysis path actually calls it, and
# that wiring is one line in one function -- exactly the kind of thing a
# refactor drops silently. Read rather than imported: `match.py` pulls in the
# engine, and this test has no other reason to need it.
source = (REPO / "gvanalysis" / "match.py").read_text()
body = source.split("def _worker_init(", 1)[-1].split("\ndef ", 1)[0]
check("watch_parent()" in body,
      "_worker_init calls watch_parent(), so real pool workers are guarded")

print()
print(f"{_checks - _failures}/{_checks} checks passed")
sys.exit(1 if _failures else 0)
