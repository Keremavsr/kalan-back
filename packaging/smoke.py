"""Smoke test of a built ``kalan`` executable: ``python packaging/smoke.py dist/kalan[.exe]``.

Checks ``--version`` and ``--help``, the sidecar contract of ``serve --port 0 --print-port`` (one ready
line, ``/health`` answers, exit 0 when stdin closes), a PDF import plus ``--json`` reads into a temporary
data folder, and that ``mcp`` lists its read-only tools over stdio. Uses only synthetic fixtures.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE, SECOND = sorted((ROOT / "tests" / "fixtures" / "garanti").glob("gb-*.pdf"))[:2]


def check(cond: bool, what: str) -> None:
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        raise SystemExit(1)


def run(exe: str, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)


def serve(exe: str, env: dict[str, str]) -> None:
    proc = subprocess.Popen([exe, "serve", "--port", "0", "--print-port"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        ready = json.loads(proc.stdout.readline())
        check(ready.get("event") == "ready" and ready.get("port", 0) > 0, f"serve ready line {ready}")
        with urllib.request.urlopen(f"http://127.0.0.1:{ready['port']}/health", timeout=10) as resp:
            health = json.loads(resp.read())
        check(health.get("status") == "ok", f"/health {health}")
        proc.stdin.close()
        check(proc.wait(timeout=30) == 0, "serve exits 0 when stdin closes")
        check(proc.stdout.read() == "", "nothing else on stdout")
    finally:
        if proc.poll() is None:
            proc.kill()


def mcp_tools(exe: str, env: dict[str, str]) -> set[str]:
    from mcp import Client, StdioServerParameters

    async def go() -> set[str]:
        async with Client(StdioServerParameters(command=exe, args=["mcp"], env=env), read_timeout_seconds=60) as c:
            return {t.name for t in (await c.list_tools()).tools}

    return asyncio.run(go())


def encrypted_import(exe: str, env: dict[str, str], folder: Path) -> None:
    """An AES-256 encrypted copy of another fixture: refused without the password, stored with it.

    Needs PyMuPDF in the Python that runs this script (a dev dependency, never bundled); skipped without it.
    """
    try:
        import pymupdf
    except ImportError:
        print("skip  encrypted PDF import (PyMuPDF not installed here)")
        return
    password = "ornek-9999"  # invented
    locked = folder / "locked.pdf"
    with pymupdf.open(str(SECOND)) as doc:
        locked.write_bytes(doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password,
                                       owner_pw=password + "-owner"))
    refused = run(exe, "import", str(locked), "--no-ai", "--json", env=env)
    check(refused.returncode == 1 and json.loads(refused.stdout)["error"]["code"] == "pdf_password_required",
          "encrypted PDF without password is refused")
    opened = run(exe, "import", str(locked), "--no-ai", "--password", password, "--json", env=env)
    check(opened.returncode == 0 and json.loads(opened.stdout)["kind"] == "statement",
          f"encrypted PDF import with --password {opened.stderr.strip()}")


def main(exe: str) -> int:
    exe = str(Path(exe).resolve())
    with tempfile.TemporaryDirectory() as data:
        env = {**os.environ, "KALAN_DATA_DIR": data, "PYTHONIOENCODING": "utf-8"}
        version = run(exe, "--version", env=env)
        check(version.returncode == 0 and version.stdout.startswith("kalan "), f"--version {version.stdout.strip()}")
        helptext = run(exe, "--help", env=env)
        check(helptext.returncode == 0 and "takvim" in helptext.stdout and "mcp" in helptext.stdout, "--help")
        serve(exe, env)
        imported = run(exe, "import", str(FIXTURE), "--no-ai", "--json", env=env)
        check(imported.returncode == 0 and json.loads(imported.stdout)["kind"] == "statement",
              f"import a PDF {imported.stderr.strip()}")
        diag = run(exe, "teshis", str(FIXTURE), env=env)
        check(diag.returncode == 0 and "--- shapes" in diag.stdout, f"teshis {diag.stderr.strip()}")
        encrypted_import(exe, env, Path(data))
        for cmd in ("statements", "takvim", "ay", "uyarilar", "abonelikler", "kategori"):
            out = run(exe, cmd, "--json", env=env)
            check(out.returncode == 0 and isinstance(json.loads(out.stdout), dict), f"{cmd} --json")
        check({"installment_schedule", "category_rules"} <= mcp_tools(exe, env), "mcp lists its tools")
    print("smoke test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python packaging/smoke.py <path to built kalan executable>")
    raise SystemExit(main(sys.argv[1]))
