"""Windows operations on one explicitly selected session file.

This module does not check ACLs or coordinate other programs. Callers must
establish those independently. It opens a fixed UUID-named file without
following reparse points, reads it through that handle, and requests deletion
through the same handle. It never enumerates or recursively removes files.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import os
import uuid


if os.name != "nt":
    raise RuntimeError("Windows is required")

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
_kernel.ReadFile.restype = wintypes.BOOL
_kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               wintypes.LPVOID, wintypes.DWORD]
_kernel.SetFileInformationByHandle.restype = wintypes.BOOL


class _Info(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD),
                ("creation", wintypes.FILETIME), ("access", wintypes.FILETIME),
                ("write", wintypes.FILETIME), ("volume", wintypes.DWORD),
                ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD)]


_kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Info)]
_kernel.GetFileInformationByHandle.restype = wintypes.BOOL
_INVALID = ctypes.c_void_p(-1).value
_OPEN_EXISTING = 3
_REPARSE = 0x400
_DIRECTORY = 0x10
_READ_ATTRIBUTES = 0x80
_READ_CONTROL = 0x20000
_READ = 0x80000000
_DELETE = 0x10000
_SHARE_READ = 1
_SHARE_WRITE = 2
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO = 4


class UnsafeSessionFile(Exception):
    """A fixed, non-sensitive reason; never includes path or contents."""


def _open(path: Path, *, directory: bool):
    access = (_READ_ATTRIBUTES | _READ_CONTROL) if directory else (_READ | _DELETE | _READ_CONTROL)
    # Omitting FILE_SHARE_DELETE protects every held ancestor from replacement.
    # Omitting FILE_SHARE_WRITE for the target prevents concurrent writers.
    share = (_SHARE_READ | _SHARE_WRITE) if directory else _SHARE_READ
    flags = _OPEN_REPARSE_POINT | (_BACKUP_SEMANTICS if directory else 0)
    handle = _kernel.CreateFileW(str(path), access, share, None, _OPEN_EXISTING, flags, None)
    if handle == _INVALID:
        raise UnsafeSessionFile("open_failed")
    try:
        info = _Info()
        if not _kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise UnsafeSessionFile("identity_unavailable")
        if info.attributes & _REPARSE:
            raise UnsafeSessionFile("reparse_point")
        if bool(info.attributes & _DIRECTORY) is not directory:
            raise UnsafeSessionFile("unexpected_file_type")
        if not directory and (info.links != 1 or not (info.index_high or info.index_low)):
            raise UnsafeSessionFile("identity_or_links_unconfirmed")
        return handle, info
    except BaseException:
        _kernel.CloseHandle(handle)
        raise


def read_then_mark_exact_session_for_deletion(directory: Path, session: str, *, max_bytes=2_000_000,
                                               validate_before_delete=None, validate_directory=None,
                                               validate_file=None) -> bytes:
    """Read only the named session and mark that same object for deletion.

    Preconditions outside this function include confirmed exit, unique request,
    trusted client and directory ACLs, and independently established exclusivity.
    Failure is conservative and never initiates a path-based fallback deletion.
    Success establishes that deletion was requested on the handle, not that
    every other handle has closed or that any sidecar file was removed.
    """
    if not isinstance(session, str):
        raise UnsafeSessionFile("invalid_session")
    try:
        parsed = uuid.UUID(session)
    except (ValueError, AttributeError):
        raise UnsafeSessionFile("invalid_session") from None
    if str(parsed) != session or type(max_bytes) is not int or not 0 <= max_bytes <= 10_000_000:
        raise UnsafeSessionFile("invalid_session")
    if not isinstance(directory, Path) or not directory.is_absolute():
        raise UnsafeSessionFile("invalid_directory")
    if any(part in (".", "..") for part in directory.parts):
        raise UnsafeSessionFile("invalid_directory")
    handles = []
    try:
        # Hold every directory until deletion is requested on the file handle.
        current = Path(directory.anchor)
        handle, _ = _open(current, directory=True)
        handles.append(handle)
        if validate_directory is not None and validate_directory(current, handle) is not True:
            raise UnsafeSessionFile("directory_acl_unconfirmed")
        for component in directory.parts[1:]:
            current = current / component
            handle, _ = _open(current, directory=True)
            handles.append(handle)
            if validate_directory is not None and validate_directory(current, handle) is not True:
                raise UnsafeSessionFile("directory_acl_unconfirmed")
        handle, info = _open(current / (session + ".jsonl"), directory=False)
        handles.append(handle)
        if validate_file is not None and validate_file(handle) is not True:
            raise UnsafeSessionFile("file_acl_unconfirmed")
        size = (info.size_high << 32) | info.size_low
        if size > max_bytes:
            raise UnsafeSessionFile("session_too_large")
        data = bytearray()
        while len(data) < size:
            amount = min(65536, size - len(data))
            buf = ctypes.create_string_buffer(amount)
            read = wintypes.DWORD()
            if not _kernel.ReadFile(handle, buf, amount, ctypes.byref(read), None) or not read.value:
                raise UnsafeSessionFile("read_failed")
            data.extend(buf.raw[:read.value])
        if validate_before_delete is not None and validate_before_delete(bytes(data)) is not True:
            raise UnsafeSessionFile("session_identity_unconfirmed")
        disposition = ctypes.c_ubyte(1)
        if not _kernel.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO,
                                                   ctypes.byref(disposition), ctypes.sizeof(disposition)):
            raise UnsafeSessionFile("delete_failed")
        return bytes(data)
    finally:
        close_failed = False
        for handle in reversed(handles):
            if not _kernel.CloseHandle(handle):
                close_failed = True
        if close_failed:
            # Do not report a successful cleanup when a handle may remain open.
            raise UnsafeSessionFile("handle_close_unconfirmed")
