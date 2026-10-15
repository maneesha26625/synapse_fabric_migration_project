"""Whether a process still runs: what decides if a folder, a lease or a run belongs to a living API.

A process id alone is not enough: once a process ends, the system gives its id to the next one, so a crashed
API's lease would look held by whatever got its id, for ever. A process is named by its id and when it
started (``started``), which no later process shares.
"""

from __future__ import annotations

import os
from typing import Optional


def _kernel32():
    import ctypes  # noqa: PLC0415 - Windows only
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return ctypes, wintypes, kernel32


_QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION


def alive(pid: int) -> bool:
    """Whether a process with this id still runs. In doubt, yes: what it owns is kept rather than taken from it."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        ctypes, wintypes, kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_QUERY, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no process has this id
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0 only asks whether it exists (never sent on Windows, where it would end the process)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def started(pid: int) -> Optional[int]:
    """When the process with this id started, as an opaque number; None when that cannot be told here."""
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            ctypes, wintypes, kernel32 = _kernel32()
        except (ImportError, OSError, AttributeError):
            return None
        handle = kernel32.OpenProcess(_QUERY, False, pid)
        if not handle:
            return None
        try:
            created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
            if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel),
                                            ctypes.byref(user)):
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read()
        # The command name (field 2) is in parentheses and may hold spaces and parentheses itself; the start time is
        # field 22, the 20th after it.
        return int(data[data.rindex(b")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


#: This process, as ``started`` names it.
SELF_STARTED = started(os.getpid())


def same(pid: int, when: Optional[int]) -> bool:
    """Whether the process that had id ``pid`` and started at ``when`` still runs. In doubt, yes.

    ``when`` None (a record written before start times were kept, or a system that cannot tell) falls back to
    whether any process has the id."""
    if not alive(pid):
        return False
    if when is None:
        return True
    now = SELF_STARTED if pid == os.getpid() else started(pid)
    return now is None or now == when
