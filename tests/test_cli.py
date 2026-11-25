"""``kalan serve`` as an Electron sidecar: free port, ready line on stdout, exit when stdin closes."""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import urllib.request

import pytest

from kalan.cli import NoTraceback, log_config, main


def _reader(stream, q: queue.Queue) -> None:
    for line in stream:
        q.put(line)
    q.put(None)


def test_sidecar_ready_line_and_exit_on_stdin_close(tmp_path):
    env = {**os.environ, "KALAN_DATA_DIR": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "kalan.cli", "serve", "--port", "0", "--print-port"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
    )
    out: queue.Queue = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, out), daemon=True).start()
    try:
        ready = json.loads(out.get(timeout=60))
        assert set(ready) == {"event", "port"} and ready["event"] == "ready"
        assert isinstance(ready["port"], int) and ready["port"] > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{ready['port']}/health", timeout=10) as resp:
            assert json.loads(resp.read()) == {"status": "ok", "version": "0.1.0"}
        proc.stdin.close()
        assert proc.wait(timeout=30) == 0
        assert out.get(timeout=10) is None  # stdout held exactly one line
        stderr = proc.stderr.read()
        assert "stdin closed" in stderr and "GET /health" in stderr
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_port_validation():
    with pytest.raises(SystemExit):
        main(["serve", "--port", "70000"])


def test_traceback_filter_keeps_only_the_type():
    try:
        raise ValueError("statement text that must not be logged")
    except ValueError:
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
                                   (), sys.exc_info())
    assert NoTraceback().filter(record)
    assert record.exc_info is None and record.getMessage() == "Exception in ASGI application (ValueError)"


def test_all_log_handlers_use_stderr():
    cfg = log_config()
    assert all(h["stream"] == "ext://sys.stderr" for h in cfg["handlers"].values())
    assert all("no_traceback" in h["filters"] for h in cfg["handlers"].values())
