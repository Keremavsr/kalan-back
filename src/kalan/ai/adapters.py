"""One adapter per model provider.

Every adapter turns one prompt into the model's raw text answer. CLI adapters run the user's own
installed tool in an empty temporary folder; HTTP adapters speak the OpenAI chat completions API.
Detection never sends a prompt: a CLI is available when its executable is on PATH and ``--version``
succeeds; an HTTP provider is available when it is configured (and, for ``local``, answers).

Secrets: the API key of ``openai-compatible`` is read from the environment or the OS keyring at call
time. It is never stored in the database, logged, or put in an error message.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 180.0
VERSION_TIMEOUT = 15.0
KEYRING_SERVICE = "kalan"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class AdapterError(RuntimeError):
    """The provider could not produce an answer (missing tool, exit code, timeout, HTTP error)."""


@dataclass
class Detection:
    available: bool
    detail: str


class Adapter(ABC):
    id: str
    name: str

    @abstractmethod
    def detect(self) -> Detection: ...

    @abstractmethod
    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str: ...


# -- CLI adapters ---------------------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started (``.cmd`` shims and node CLIs spawn grandchildren)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_NO_WINDOW, check=False, timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    proc.kill()


def _run(
    args: list[str], stdin: str | None, timeout: float, cwd: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run a CLI with a hard timeout. Without ``stdin`` the child gets an empty stdin, never ours."""
    name = Path(args[0]).stem
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=None if env is None else {**os.environ, **env},
            creationflags=_NO_WINDOW,
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        raise AdapterError(f"could not start {name}: {exc.strerror or exc}") from None
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=5)
        raise AdapterError(f"{name} did not answer within {int(timeout)} s") from None
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


class CliAdapter(Adapter):
    executable: str

    def which(self) -> str | None:
        return shutil.which(self.executable)

    def detect(self) -> Detection:
        path = self.which()
        if path is None:
            return Detection(False, f"{self.executable} not found on PATH")
        try:
            proc = _run([path, "--version"], None, VERSION_TIMEOUT)
        except AdapterError as exc:
            return Detection(False, str(exc))
        if proc.returncode != 0:
            return Detection(False, f"{self.executable} --version exited with code {proc.returncode}")
        version = (proc.stdout or proc.stderr).strip().splitlines()
        label = version[0].strip()[:80] if version else self.executable
        return Detection(True, f"{label} found on PATH")

    def _exe(self) -> str:
        path = self.which()
        if path is None:
            raise AdapterError(f"{self.executable} not found on PATH")
        return path

    def _checked(self, proc: subprocess.CompletedProcess) -> str:
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-300:]
            raise AdapterError(f"{self.executable} exited with code {proc.returncode}" + (f": {tail}" if tail else ""))
        return proc.stdout


class ClaudeCli(CliAdapter):
    id = "claude-cli"
    name = "Claude Code"
    executable = "claude"

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        with tempfile.TemporaryDirectory(prefix="kalan-") as cwd:
            out = self._checked(_run([self._exe(), "-p", "--output-format", "json"], prompt, timeout, cwd))
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            return out
        if isinstance(envelope, dict) and "result" in envelope:
            if envelope.get("is_error"):
                raise AdapterError("claude reported an error: " + str(envelope.get("result", ""))[:300])
            return str(envelope["result"])
        return out


class CodexCli(CliAdapter):
    id = "codex-cli"
    name = "Codex CLI"
    executable = "codex"

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        with tempfile.TemporaryDirectory(prefix="kalan-") as cwd:
            answer = Path(cwd) / "answer.txt"
            args = [self._exe(), "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-o", str(answer), "-"]
            self._checked(_run(args, prompt, timeout, cwd))
            if not answer.exists():
                raise AdapterError("codex wrote no answer file")
            return answer.read_text(encoding="utf-8")


class AgyCli(CliAdapter):
    id = "agy-cli"
    name = "Antigravity CLI"
    executable = "agy"

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        with tempfile.TemporaryDirectory(prefix="kalan-") as cwd:
            return self._checked(_run([self._exe(), "-p", prompt], None, timeout, cwd))


class GeminiCli(CliAdapter):
    id = "gemini-cli"
    name = "Gemini CLI"
    executable = "gemini"

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        # The prompt goes through stdin (no command-line length limit); -p only switches to headless mode.
        # The empty temp folder holds nothing to protect, so it is trusted for this one run.
        with tempfile.TemporaryDirectory(prefix="kalan-") as cwd:
            args = [self._exe(), "-p", "Follow the instructions given on standard input."]
            return self._checked(_run(args, prompt, timeout, cwd, env={"GEMINI_CLI_TRUST_WORKSPACE": "true"}))


# -- HTTP adapters (OpenAI chat completions) ------------------------------------------------------


def _keyring_secret(username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, username)
    except Exception:  # no keyring package or no backend
        return None


def _http_json(url: str, body: dict[str, Any] | None, headers: dict[str, str], timeout: float) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AdapterError(f"HTTP {exc.code} from {urlparse(url).netloc}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise AdapterError(f"could not reach {urlparse(url).netloc}: {reason}") from None
    except json.JSONDecodeError:
        raise AdapterError(f"{urlparse(url).netloc} did not answer with JSON") from None


class OpenAICompatible(Adapter):
    """Any OpenAI-compatible endpoint. Env: ``KALAN_OPENAI_BASE_URL``, ``KALAN_OPENAI_MODEL``,
    ``KALAN_OPENAI_API_KEY`` (or keyring service ``kalan``, user ``openai-compatible``)."""

    id = "openai-compatible"
    name = "OpenAI-compatible API"
    env_prefix = "KALAN_OPENAI"

    def base_url(self) -> str | None:
        url = os.environ.get(f"{self.env_prefix}_BASE_URL")
        return url.rstrip("/") if url else None

    def model(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_MODEL")

    def api_key(self) -> str | None:
        return os.environ.get(f"{self.env_prefix}_API_KEY") or _keyring_secret(self.id)

    def detect(self) -> Detection:
        url, model = self.base_url(), self.model()
        if not url or not model:
            return Detection(False, f"set {self.env_prefix}_BASE_URL and {self.env_prefix}_MODEL")
        if not self.api_key():
            return Detection(False, f"no API key (set {self.env_prefix}_API_KEY or the OS keyring)")
        return Detection(True, f"{urlparse(url).netloc}, model {model}")

    def _headers(self) -> dict[str, str]:
        key = self.api_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        url, model = self.base_url(), self.model()
        if not url or not model:
            raise AdapterError(f"{self.name} is not configured")
        body = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        data = _http_json(f"{url}/chat/completions", body, self._headers(), timeout)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise AdapterError("the API answer has no choices[0].message.content") from None


class LocalModel(OpenAICompatible):
    """A local OpenAI-compatible server (llama.cpp, Ollama, LM Studio). Env: ``KALAN_LOCAL_URL``
    (default ``http://127.0.0.1:11434/v1``), optional ``KALAN_LOCAL_MODEL``. Only localhost is accepted."""

    id = "local"
    name = "Local model"
    env_prefix = "KALAN_LOCAL"
    default_url = "http://127.0.0.1:11434/v1"

    def base_url(self) -> str | None:
        return (os.environ.get("KALAN_LOCAL_URL") or self.default_url).rstrip("/")

    def api_key(self) -> str | None:
        return None

    def _is_local(self) -> bool:
        return urlparse(self.base_url() or "").hostname in LOCAL_HOSTS

    def _first_model(self, timeout: float) -> str:
        data = _http_json(f"{self.base_url()}/models", None, {}, timeout)
        try:
            return str(data["data"][0]["id"])
        except (KeyError, IndexError, TypeError):
            raise AdapterError("the local server lists no model") from None

    def model(self) -> str | None:
        return os.environ.get("KALAN_LOCAL_MODEL")

    def detect(self) -> Detection:
        if not self._is_local():
            return Detection(False, "KALAN_LOCAL_URL must point at localhost")
        try:
            model = self.model() or self._first_model(2.0)
        except AdapterError as exc:
            return Detection(False, str(exc))
        return Detection(True, f"{urlparse(self.base_url() or '').netloc}, model {model}")

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        if not self._is_local():
            raise AdapterError("KALAN_LOCAL_URL must point at localhost")
        model = self.model() or self._first_model(10.0)
        body = {"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        data = _http_json(f"{self.base_url()}/chat/completions", body, {}, timeout)
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise AdapterError("the local server's answer has no choices[0].message.content") from None


# -- test adapter ---------------------------------------------------------------------------------


class FakeAdapter(Adapter):
    """For tests: answers from a list (or a function of the prompt) and records every prompt."""

    def __init__(
        self,
        answers: list[str] | Callable[[str], str] | None = None,
        *,
        id: str = "fake",
        available: bool = True,
        delay: float = 0.0,
    ) -> None:
        self.id = id
        self.name = "Fake model"
        self.available = available
        self.answers = answers if answers is not None else []
        self.delay = delay
        self.prompts: list[str] = []
        self.release = threading.Event()
        self.release.set()

    def detect(self) -> Detection:
        return Detection(self.available, "fake adapter" if self.available else "fake adapter switched off")

    def complete(self, prompt: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        self.prompts.append(prompt)
        self.release.wait(timeout)
        if self.delay:
            time.sleep(self.delay)
        if callable(self.answers):
            return self.answers(prompt)
        if not self.answers:
            raise AdapterError("fake adapter has no answer left")
        answer = self.answers.pop(0)
        if answer.startswith("!error:"):
            raise AdapterError(answer.removeprefix("!error:"))
        return answer


# -- registry -------------------------------------------------------------------------------------


def default_adapters() -> list[Adapter]:
    return [ClaudeCli(), CodexCli(), AgyCli(), GeminiCli(), OpenAICompatible(), LocalModel()]


class ProviderRegistry:
    """The adapters on offer, with detection results cached for ``ttl`` seconds."""

    def __init__(self, adapters: list[Adapter] | None = None, ttl: float = 30.0) -> None:
        self.adapters = adapters if adapters is not None else default_adapters()
        self.ttl = ttl
        self._cache: tuple[float, dict[str, Detection]] | None = None
        self._lock = threading.Lock()

    def get(self, provider_id: str) -> Adapter | None:
        return next((a for a in self.adapters if a.id == provider_id), None)

    def detect_all(self, refresh: bool = False) -> dict[str, Detection]:
        with self._lock:
            if not refresh and self._cache and time.monotonic() - self._cache[0] < self.ttl:
                return self._cache[1]
            result = {}
            for a in self.adapters:
                try:
                    result[a.id] = a.detect()
                except Exception as exc:  # a broken tool must not break GET /providers
                    result[a.id] = Detection(False, f"detection failed: {type(exc).__name__}")
            self._cache = (time.monotonic(), result)
            return result
