"""Is a process still running? Standard library only (``ctypes`` on Windows, ``os.kill(pid, 0)`` elsewhere)."""

from __future__ import annotations

import os
import sys

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int | None) -> bool:
    """True when a process with this id exists. A reused id reads as alive; callers pair this with a heartbeat."""
    if pid is None or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists but not ours to open
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: do not fail someone else's job
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


__all__ = ["pid_alive"]
