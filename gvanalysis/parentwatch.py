# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis
"""Make a pool worker die with the process that started it.

A `ProcessPoolExecutor` cleans up after itself when the parent exits normally:
`shutdown()` feeds each worker a `None`, the worker returns, the process ends.
None of that happens when the parent dies *hard* -- SIGKILL, a force-quit, a
crash, `launchctl kill`, a laptop that was shut down rather than logged out.
The workers are then blocked in `call_queue.get()` on a queue nobody will ever
write to again, they reparent to PID 1, and they stay there holding their
analyzers and weights until the machine reboots.

**This is not theoretical and the cost is not small.** Thirty-seven of these
were found on the analysis Mac in September 2026, in three distinct cohorts
days apart, holding roughly 19 GB between them. On a 128 GB box that is
absorbable; the helper (`gammonview-helper`) runs this same code on other
people's laptops, where a 16 GB machine losing 4 GB to processes that do
nothing is the difference between "the analysis is a bit slow" and "my
computer is broken", and the user has no idea what to look for.

Why the queue does not simply reach EOF, since that is the obvious question:
under the `spawn` start method every worker holds its own handle to the call
queue's pipe, so the write end stays open among the survivors after the parent
is gone. There is nothing to notice. The worker blocks forever, correctly, on
a promise that will not be kept.

So the worker has to watch for the death itself, and `multiprocessing` already
built the thing that notices. `parent_process()` returns a handle whose
sentinel is a pipe the *parent alone* holds the other end of -- no sibling has
a copy, which is exactly the property the call queue lacks -- and the kernel
closes it when the parent dies, however it dies. Joining it is a blocking wait
that costs one idle thread and no polling.

Deliberately not `prctl(PR_SET_PDEATHSIG)`: Linux-only, and the machines that
matter here are macOS and Windows laptops. Deliberately not an `os.getppid()`
poll either -- it has no Windows equivalent, and the stdlib answer works on all
three.
"""

from __future__ import annotations

import multiprocessing
import os
import threading

#: Set once a watcher is running in this process. `_worker_init` runs once per
#: worker, but a pool that recycles workers (`max_tasks_per_child`) or a caller
#: that re-enters would otherwise stack up threads that all wait on the same
#: sentinel and all call `os._exit`.
_watching = False
_lock = threading.Lock()


def watch_parent() -> bool:
    """Exit this process when its parent dies. No-op outside a child process.

    Returns True if a watcher is now running -- for tests, and so a caller can
    tell "watching" from "this is the main process" without inspecting
    `multiprocessing` itself.

    The exit is `os._exit`, and both halves of that matter. It must be `_exit`
    rather than `sys.exit` because this runs on a non-main thread, where
    `SystemExit` unwinds that thread alone and leaves the process exactly as
    stuck as before. And it must be immediate rather than graceful because
    there is nothing left to be graceful *toward*: the result queue leads to a
    dead process, the analyzer is a C++ extension that may be mid-evaluation,
    and every atexit hook here was registered to tidy up for a parent that is
    already gone.
    """
    global _watching

    parent = multiprocessing.parent_process()
    if parent is None:
        # The main process. It has a parent, of course, but not one whose death
        # says anything about whether this work is still wanted.
        return False

    with _lock:
        if _watching:
            return True
        _watching = True

    def wait() -> None:
        parent.join()
        os._exit(1)

    # Daemon, so a normally-shutting-down worker is not held open by a thread
    # waiting for a parent that is about to outlive it by microseconds.
    threading.Thread(target=wait, name="gvanalysis-parent-watch", daemon=True).start()
    return True
