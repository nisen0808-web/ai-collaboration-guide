"""Conservative Linux reader for one Grok session's metadata files.

The caller binds the UUID to a new request, confirms process exit, and parses
the returned bytes. No session history, credentials or other tasks are read,
created, modified or removed. On platforms without the required security
primitives this module reports unavailable, not success.

Directory file descriptors reduce symlink and replacement races; they cannot
protect against every malicious process running as the same operating-system
user. Use a private, dedicated profile and working directory.
"""
from __future__ import annotations

import errno
import os
from pathlib import PurePosixPath
import stat
import sys
from urllib.parse import quote
import uuid

_MAX_BYTES = 1_000_000


class _Unsafe(Exception):
    pass


def _inputs(profile_dir, work_dir, session_id):
    if not all(isinstance(value, str) and value and '\x00' not in value
               for value in (profile_dir, work_dir, session_id)):
        raise _Unsafe()
    if str(uuid.UUID(session_id)) != session_id:
        raise _Unsafe()
    for path in (profile_dir, work_dir):
        # A stable lexical path is required; symlinks are rejected separately
        # through O_NOFOLLOW while holding each directory handle.
        if not path.startswith('/') or path.startswith('//') or os.path.normpath(path) != path:
            raise _Unsafe()
        if path == '/' or any(part in ('.', '..') for part in PurePosixPath(path).parts):
            raise _Unsafe()
    encoded = quote(work_dir, safe='')
    if not encoded or len(encoded.encode('ascii')) > 255:
        # The CLI uses a slug/hash for long working paths. We do not guess it.
        raise _Unsafe()
    return encoded


def _supported():
    return (sys.platform.startswith('linux') and hasattr(os, 'O_NOFOLLOW') and
            hasattr(os, 'O_DIRECTORY') and hasattr(os, 'getxattr') and
            os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd)


def _no_acl(fd):
    # A named-user ACL can expose data even if the owner/group bits look safe.
    # An unknown ACL is not proof of privacy; reject present or unreadable ACLs.
    try:
        os.getxattr(fd, 'system.posix_acl_access')
    except OSError as exc:
        if exc.errno in (errno.ENODATA, getattr(errno, 'ENOATTR', errno.ENODATA)):
            return True
        return False
    return False


def _directory_ok(fd, *, private):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
        raise _Unsafe()
    mode = stat.S_IMODE(info.st_mode)
    if private:
        if info.st_uid != os.geteuid() or mode & 0o077 or not _no_acl(fd):
            raise _Unsafe()
    elif mode & 0o022:
        # A root-owned sticky directory such as /tmp protects the private
        # child from deletion by a different user. The leaf must remain private.
        if info.st_uid != 0 or not (mode & stat.S_ISVTX):
            raise _Unsafe()
    return info


def _open_dir(name, *, parent=None):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    return os.open(name, flags, dir_fd=parent) if parent is not None else os.open(name, flags)


def _chain(path, opened):
    """Hold each real component from root through a private leaf."""
    parts = PurePosixPath(path).parts
    current = _open_dir('/')
    opened.append(current)
    _directory_ok(current, private=False)
    for index, part in enumerate(parts[1:]):
        child = _open_dir(part, parent=current)
        opened.append(child)
        _directory_ok(child, private=index == len(parts[1:]) - 1)
        current = child
    return current


def _child_dir(parent, name, opened):
    fd = _open_dir(name, parent=parent)
    opened.append(fd)
    _directory_ok(fd, private=True)
    return fd


def _close(opened):
    success = True
    for fd in reversed(opened):
        try:
            os.close(fd)
        except OSError:
            success = False
    return success


def _missing(parent, name):
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def preflight(profile_dir, work_dir, session_id):
    """Return True only for a new UUID and an exact safely addressable cwd.

    This is a point-in-time absence check, not a lock against the same user.
    Caller must keep its UUID unique and retain the original task/job binding.
    """
    opened = []
    result = False
    try:
        if not _supported():
            return False
        encoded = _inputs(profile_dir, work_dir, session_id)
        profile = _chain(profile_dir, opened)
        _chain(work_dir, opened)
        if _missing(profile, 'sessions'):
            result = True
        else:
            sessions = _child_dir(profile, 'sessions', opened)
            if _missing(sessions, encoded):
                result = True
            else:
                project = _child_dir(sessions, encoded, opened)
                result = _missing(project, session_id)
    except (OSError, ValueError, TypeError, _Unsafe):
        result = False
    finally:
        if not _close(opened):
            result = False
    return result


def _read_file(parent, name, max_bytes):
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(name, flags, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid() or
                stat.S_IMODE(before.st_mode) & 0o077 or before.st_nlink != 1 or
                before.st_size > max_bytes or not _no_acl(fd)):
            raise _Unsafe()
        chunks, count = [], 0
        while True:
            data = os.read(fd, min(65536, max_bytes + 1 - count))
            if not data:
                break
            chunks.append(data)
            count += len(data)
            if count > max_bytes:
                raise _Unsafe()
        after = os.fstat(fd)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_nlink) or
                count != before.st_size):
            raise _Unsafe()
        return b''.join(chunks)
    finally:
        os.close(fd)


def read_records(profile_dir, work_dir, session_id, *, max_bytes=_MAX_BYTES):
    """Read only this session's bounded summary and usage bytes or return None.

    This function does not decide whether the records attest any model. The
    caller must separately require preflight and confirmed process exit, check
    session/cwd binding and parse without emitting raw metadata.
    """
    opened = []
    result = None
    try:
        if not _supported() or type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BYTES:
            return None
        encoded = _inputs(profile_dir, work_dir, session_id)
        profile = _chain(profile_dir, opened)
        _chain(work_dir, opened)
        sessions = _child_dir(profile, 'sessions', opened)
        project = _child_dir(sessions, encoded, opened)
        session = _child_dir(project, session_id, opened)
        result = (_read_file(session, 'summary.json', max_bytes),
                  _read_file(session, 'usage.json', max_bytes))
    except (OSError, ValueError, TypeError, _Unsafe):
        result = None
    finally:
        if not _close(opened):
            result = None
    return result
