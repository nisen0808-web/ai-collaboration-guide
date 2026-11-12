"""Conservative read-only Windows ACL check on an already-open directory.

This check is not authorization to launch a provider. Unknown ACL shapes
fail closed. It returns a boolean and does not print SIDs or paths.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

if os.name != "nt":
    raise RuntimeError("Windows is required")

_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel.GetCurrentProcess.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.LocalFree.argtypes = [wintypes.HLOCAL]
_kernel.LocalFree.restype = wintypes.HLOCAL
_adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_adv.OpenProcessToken.restype = wintypes.BOOL
_adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_adv.GetTokenInformation.restype = wintypes.BOOL
_adv.GetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                 ctypes.POINTER(ctypes.c_void_p)]
_adv.GetSecurityInfo.restype = wintypes.DWORD
_adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
_adv.GetSecurityDescriptorControl.restype = wintypes.BOOL
_adv.GetAclInformation.argtypes = [ctypes.c_void_p, wintypes.LPVOID, wintypes.DWORD, ctypes.c_int]
_adv.GetAclInformation.restype = wintypes.BOOL
_adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
_adv.GetAce.restype = wintypes.BOOL
_adv.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_adv.EqualSid.restype = wintypes.BOOL
_adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
_adv.ConvertStringSidToSidW.restype = wintypes.BOOL


class _AclSize(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]


class _AceHeader(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", wintypes.WORD)]


def protected_private_acl(handle, *, require_protected: bool = True, allow_inherited: bool = False) -> bool:
    """Allow only explicit owner/current-user/SYSTEM/Administrators ACEs.

    Deny ACEs, object/callback ACEs, null DACLs, or an owner
    different from the current account are conservative failures. This is
    intentionally stricter than normal Windows permissions and is not a proof
    that another program running as the same user cannot access the directory.
    Inherited entries may be accepted only for children when the caller has
    independently checked the protected root and this child's complete ACL.
    """
    token = wintypes.HANDLE()
    descriptor = ctypes.c_void_p()
    sid_allocations = []
    try:
        if not _adv.OpenProcessToken(_kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        required = wintypes.DWORD()
        _adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not 0 < required.value <= 1_000_000:
            return False
        data = ctypes.create_string_buffer(required.value)
        if not _adv.GetTokenInformation(token, 1, data, required.value, ctypes.byref(required)):
            return False
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is PSID.
        user_sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not user_sid:
            return False
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        if _adv.GetSecurityInfo(handle, 1, 0x1 | 0x4, ctypes.byref(owner), None,
                                ctypes.byref(dacl), None, ctypes.byref(descriptor)) != 0:
            return False
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if (not descriptor.value or not owner.value or not dacl.value or
                not _adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)) or
                (require_protected and not (control.value & 0x1000)) or not _adv.EqualSid(owner, user_sid)):
            return False
        allowed = [ctypes.c_void_p(user_sid)]
        for text in ("S-1-5-18", "S-1-5-32-544", "S-1-3-4"):
            sid = ctypes.c_void_p()
            if not _adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
                return False
            sid_allocations.append(sid)
            allowed.append(sid)
        info = _AclSize()
        if not _adv.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2) or not 0 < info.count <= 1024:
            return False
        saw_user = False
        for index in range(info.count):
            ace = ctypes.c_void_p()
            if not _adv.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                return False
            header = ctypes.cast(ace, ctypes.POINTER(_AceHeader)).contents
            if header.kind != 0 or (header.flags & 0x10 and not allow_inherited) or header.size < 12:
                return False
            sid = ctypes.c_void_p(ace.value + 8)
            if not any(_adv.EqualSid(sid, known) for known in allowed):
                return False
            # OWNER RIGHTS represents the already verified current owner. An
            # inherit-only entry does not apply to this directory itself.
            is_current_owner = (_adv.EqualSid(sid, user_sid) or _adv.EqualSid(sid, allowed[-1]))
            saw_user = saw_user or (bool(is_current_owner) and not (header.flags & 0x08))
        return saw_user
    except (ValueError, OSError, TypeError):
        return False
    finally:
        for sid in sid_allocations:
            _kernel.LocalFree(sid)
        if descriptor.value:
            _kernel.LocalFree(descriptor)
        if token.value:
            _kernel.CloseHandle(token)
