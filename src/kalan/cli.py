"""``kalan`` command line.

``kalan serve [--port N] [--print-port]`` runs the HTTP API. The host is fixed to 127.0.0.1.
``kalan template check <file.yaml> <statement.pdf> [--quiet]`` tests a bank template on one statement.
``kalan template schema`` prints the template JSON Schema.

Sidecar mode (the Electron desktop app starts the back end as a child process):

* ``--port 0`` lets the OS pick a free port.
* ``--print-port`` writes exactly one JSON line to stdout once the server listens:
  ``{"event": "ready", "port": N}``. Nothing else is ever written to stdout (all logs go to stderr).
* When stdin is a pipe, the process exits cleanly (exit code 0) as soon as the pipe closes, so it
  never outlives a parent that died. A terminal or ``/dev/null`` stdin is not watched.

stderr carries ids, counts, paths and error type names only: tracebacks are reduced to the exception
type, because an exception message could quote statement content.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import logging
import os
import socket
import stat
import sys
import threading

from kalan import __version__
from kalan.config import DEFAULT_PORT, HOST


class NoTraceback(logging.Filter):
    """Replace exception details by the exception type name, so no message text reaches the log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            exc_type = record.exc_info[0]
            record.msg = f"{record.getMessage()} ({exc_type.__name__ if exc_type else 'error'})"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def log_config() -> dict:
    """uvicorn's logging config with every handler on stderr and the traceback filter attached."""
    from uvicorn.config import LOGGING_CONFIG

    cfg = copy.deepcopy(LOGGING_CONFIG)
    cfg.setdefault("filters", {})["no_traceback"] = {"()": "kalan.cli.NoTraceback"}
    for handler in cfg["handlers"].values():
        handler["stream"] = "ext://sys.stderr"
        handler.setdefault("filters", []).append("no_traceback")
    cfg["loggers"]["kalan"] = {"handlers": ["default"], "level": "INFO", "propagate": False}
    return cfg


def stdin_is_pipe() -> bool:
    if sys.stdin is None:
        return False
    try:
        return stat.S_ISFIFO(os.fstat(sys.stdin.fileno()).st_mode)
    except (OSError, ValueError):
        return False


def _watch_stdin(server) -> None:
    try:
        for _ in sys.stdin.buffer:
            pass
    except (OSError, ValueError):
        pass
    logging.getLogger("kalan").info("stdin closed, shutting down")
    server.should_exit = True


def serve(port: int, print_port: bool) -> int:
    import uvicorn

    from kalan.api import create_app

    class Server(uvicorn.Server):
        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            if self.started and print_port and sockets:
                sys.stdout.write(json.dumps({"event": "ready", "port": sockets[0].getsockname()[1]}) + "\n")
                sys.stdout.flush()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, port))
    except OSError as exc:
        sys.stderr.write(f"kalan: cannot listen on {HOST}:{port} ({exc.strerror or exc})\n")
        sock.close()
        return 1
    sock.set_inheritable(True)

    config = uvicorn.Config(create_app(), log_config=log_config(), log_level="info")
    server = Server(config)
    if stdin_is_pipe():
        threading.Thread(target=_watch_stdin, args=(server,), daemon=True, name="kalan-stdin").start()
    server.run(sockets=[sock])
    return 0


def _utf8_stdout() -> None:
    """Pipes and files get UTF-8 (scripts read ``--json``); a console keeps its encoding, never crashing."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    with contextlib.suppress(OSError, ValueError):
        reconfigure(encoding="utf-8") if not stream.isatty() else reconfigure(errors="replace")


def run_data_command(cmd, args: argparse.Namespace, kalan=None) -> int:
    from kalan.service import Kalan, ServiceError

    _utf8_stdout()
    try:
        k = kalan or Kalan()
        out = cmd.handler(k, args)
    except ServiceError as exc:
        if args.json:
            print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        sys.stderr.write(f"kalan: {exc.message} ({exc.code})\n")
        return 1
    if args.json:
        print(json.dumps(out.data, ensure_ascii=False, indent=2, default=str))
    else:
        for line in out.lines:
            print(line)
    return 0


def build_parser() -> tuple[argparse.ArgumentParser, dict]:
    from kalan.commands import load

    parser = argparse.ArgumentParser(prog="kalan", description="Local, installment-first credit card tracker.")
    parser.add_argument("--version", action="version", version=f"kalan {__version__}")
    parser.add_argument("--data-dir", help="folder of the kalan database (default: KALAN_DATA_DIR or the user "
                        "data folder)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    p_serve = sub.add_parser("serve", help=f"run the HTTP API on {HOST}")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT,
                         help=f"port (default {DEFAULT_PORT}; 0 picks a free port)")
    p_serve.add_argument("--print-port", action="store_true",
                         help='print one JSON line {"event":"ready","port":N} to stdout when listening')
    sub.add_parser("mcp", help="read-only MCP server on stdio, for Claude Code, Codex or Gemini CLI")
    p_tpl = sub.add_parser("template", help="work with declarative bank templates")
    tsub = p_tpl.add_subparsers(dest="template_command", required=True)
    p_check = tsub.add_parser("check", help="validate a template and run it on one statement")
    p_check.add_argument("file", help="template YAML file")
    p_check.add_argument("statement", help="statement PDF (or .txt with the extracted text)")
    p_check.add_argument("--quiet", action="store_true", help="counts and statuses only, no values")
    tsub.add_parser("schema", help="print the template JSON Schema")

    by_name = {}
    for cmd in load().values():
        p = sub.add_parser(cmd.name, aliases=list(cmd.aliases), help=cmd.help, description=cmd.help)
        for names, kwargs in cmd.args:
            p.add_argument(*names, **kwargs)
        p.add_argument("--json", action="store_true", help="print JSON (money in minor units) instead of a table")
        for name in (cmd.name, *cmd.aliases):
            by_name[name] = cmd
    return parser, by_name


def main(argv: list[str] | None = None, *, kalan=None) -> int:
    parser, data_commands = build_parser()
    args = parser.parse_args(argv)
    if args.data_dir:
        os.environ["KALAN_DATA_DIR"] = args.data_dir
    if args.command in data_commands:
        return run_data_command(data_commands[args.command], args, kalan)
    if args.command == "template":
        if args.template_command == "schema":
            from kalan.parsers.template import template_json_schema

            print(json.dumps(template_json_schema(), ensure_ascii=False, indent=2))
            return 0
        from kalan.template_check import check

        lines, code = check(args.file, args.statement, quiet=args.quiet)
        for line in lines:
            print(line)
        return code
    if args.command == "mcp":
        from kalan.mcp_server import run

        return run()
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        return serve(args.port, args.print_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
