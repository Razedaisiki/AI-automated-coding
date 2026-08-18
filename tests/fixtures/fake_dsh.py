#!/usr/bin/env python3
"""Fake 'dsh' executable for Supervisor tests — zero LLM, zero tokens.

Behaves per env vars so DshRunner/engine tests can script Parent behavior:

- FAKE_DSH_MODE
    exit0               write state (if given), print, exit 0
    exit1               write state (if given), print, exit 1
    hang                print, sleep forever (SIGTERM default -> dies)
    ignore_term_and_hang ignore SIGTERM and sleep forever (needs SIGKILL)
    term_then_exit      on SIGTERM, exit 0 (cooperative shutdown)
- FAKE_DSH_STATE       JSON string written to .agent/state.json atomically
- FAKE_DSH_STATE_PATH  override state file path (default <cwd>/.agent/state.json)
"""
import json
import os
import signal
import sys
import time


def _atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _maybe_write_state():
    state_json = os.environ.get("FAKE_DSH_STATE")
    if not state_json:
        return
    path = os.environ.get(
        "FAKE_DSH_STATE_PATH",
        os.path.join(os.getcwd(), ".agent", "state.json"),
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, state_json)


def main():
    argv = sys.argv[1:]  # ['--profile', 'headless', prompt, ...]
    prompt = " ".join(argv)
    mode = os.environ.get("FAKE_DSH_MODE", "exit0")

    print("fake_dsh argv=%r" % (argv,))
    print("fake_dsh cwd=%s" % os.getcwd())
    print("fake_dsh prompt=%s" % prompt[:500])
    sys.stdout.flush()

    if mode == "exit1":
        _maybe_write_state()
        sys.stderr.write("fake dsh exiting with code 1\n")
        sys.stderr.flush()
        sys.exit(1)

    if mode == "exit0":
        _maybe_write_state()
        sys.exit(0)

    if mode == "hang":
        _maybe_write_state()
        while True:
            time.sleep(0.5)

    if mode == "ignore_term_and_hang":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _maybe_write_state()
        while True:
            time.sleep(0.5)

    if mode == "term_then_exit":
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        while True:
            time.sleep(0.5)

    sys.exit(2)  # unknown mode


if __name__ == "__main__":
    main()