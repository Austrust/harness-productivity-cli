from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from .common import Problem, canonical, fingerprint


def default_state_root(*, platform=None, environ=None, home=None):
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform == "win32":
        base = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        # XDG relative paths are invalid; do not depend on the harness's cwd.
        base = xdg if xdg.is_absolute() else home / ".local" / "share"
    return base / "HarnessProductivity"


def _secure_keyring():
    """Select an OS store explicitly; never a chainer or keyrings.alt fallback."""
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
            backend = Keyring()
        elif sys.platform.startswith("linux"):
            from keyring.backends.SecretService import Keyring
            from keyring.errors import KeyringLocked
            import secretstorage

            class UnlockedSecretService(Keyring):
                def get_preferred_collection(self):
                    connection = secretstorage.dbus_init()
                    try:
                        collection = secretstorage.get_default_collection(connection)
                        if collection.is_locked():
                            raise KeyringLocked("Unlock the login keyring in the owner's session first")
                        return collection
                    except Exception:
                        connection.close()
                        raise

                def unlock(self, item):
                    if item.is_locked():
                        raise KeyringLocked("Unlock the secret in the owner's session first")

            backend = UnlockedSecretService()
        else:
            raise RuntimeError("Unsupported operating system")
        if backend.priority <= 0:
            raise RuntimeError("OS credential store unavailable")
        return backend
    except Exception:
        raise _store_error() from None


def _store_error():
    return Problem("credential_store_unavailable",
                   "Unlock macOS Keychain, or run Linux with an unlocked Secret Service keyring and the same D-Bus session; see ENVIRONMENT.md")


def _keyring_operation(operation, path, name, value=None):
    import keyring.errors
    try:
        backend = _secure_keyring()
        method = getattr(backend, operation + "_password")
        arguments = ("HarnessProductivity", str(path) + ":" + name)
        return method(*arguments, value) if operation == "set" else method(*arguments)
    except keyring.errors.PasswordDeleteError:
        if operation != "delete":
            raise _store_error() from None
    except Exception:
        raise _store_error() from None


def protect_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Use SIDs, not localized account names. Never invoke a shell.
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                                creationflags=0x08000000, timeout=10)
        sid = re.search(r"S-1-5-[0-9-]+", result.stdout)
        if not sid:
            raise Problem("local_storage_error", "Cannot determine current Windows user SID")
        result = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                                 "*" + sid.group() + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                                capture_output=True, creationflags=0x08000000, timeout=10)
        if result.returncode:
            raise Problem("local_storage_error", "Cannot secure the local credential directory")
    else:
        path.chmod(0o700)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class State:
    def __init__(self, root=None, profile="default"):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", profile):
            raise Problem("invalid_input", "Profile name must contain 1-64 letters, digits, underscores or hyphens")
        base = Path(root) if root else default_state_root()
        self.path = base.resolve() / profile
        self.profile = profile

    def initialize(self):
        if not self.path.exists():
            protect_directory(self.path)

    def load(self, kind):
        p = self.path / (kind + ".json")
        if not p.exists():
            raise Problem("needs_login", "Run the CLI auth command interactively first", exit_code=2)
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise Problem("local_storage_error", "Cannot read local profile configuration") from None

    def save(self, kind, data):
        self.initialize()
        atomic_json(self.path / (kind + ".json"), data)

    def secret_set(self, name, value):
        self.initialize()
        if os.name == "nt":
            data = _dpapi(value.encode(), decrypt=False)
            (self.path / (name + ".dpapi")).write_bytes(data)
        else:
            _keyring_operation("set", self.path, name, value)

    def secret_get(self, name):
        if os.name == "nt":
            p = self.path / (name + ".dpapi")
            if not p.exists():
                return None
            try:
                return _dpapi(p.read_bytes(), decrypt=True).decode()
            except Exception:
                raise Problem("needs_login", "Cannot decrypt credentials for this Windows user; run auth again", exit_code=2) from None
        return _keyring_operation("get", self.path, name)

    def secret_delete(self, name):
        if os.name == "nt":
            (self.path / (name + ".dpapi")).unlink(missing_ok=True)
        else:
            _keyring_operation("delete", self.path, name)

    @contextlib.contextmanager
    def lock(self):
        """Serialize cookie/token file access across processes and harnesses."""
        self.initialize()
        with open(self.path / "command.lock", "a+b") as fh:
            if fh.tell()==0:
                fh.write(b"0"); fh.flush()
            deadline=time.monotonic()+5
            while True:
                try:
                    fh.seek(0)
                    if os.name=="nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic()>deadline: raise Problem("busy","Another CLI operation is using this profile; retry with the same request ID") from None
                    time.sleep(.1)
            try: yield
            finally:
                fh.seek(0)
                if os.name=="nt":
                    msvcrt.locking(fh.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(fh.fileno(),fcntl.LOCK_UN)


def _dpapi(data, *, decrypt):
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "HarnessProductivity", None, None, None, 1, ctypes.byref(dest))
    if not ok:
        raise Problem("credential_store_unavailable", "Windows DPAPI failed")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


class Journal:
    """Reserve before mutation; replay only verifies. No blind second create after a timeout."""
    def __init__(self, state):
        state.initialize()
        self.path = state.path / "requests.sqlite3"
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (scope TEXT, key TEXT, fingerprint TEXT, resource TEXT, created TEXT, PRIMARY KEY(scope,key))")

    def db(self):
        return sqlite3.connect(self.path, timeout=10)

    def reserve(self, scope, key, payload, *, resource=None):
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            raise Problem("invalid_input", "A stable request_id of 1-200 characters is required for writes")
        digest = fingerprint(payload)
        resource = resource or str(uuid.uuid5(uuid.NAMESPACE_URL, scope + ":" + key)).upper()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,resource FROM requests WHERE scope=? AND key=?", (scope, key)).fetchone()
            if row:
                if row[0] != digest:
                    raise Problem("request_conflict", "This request ID was already used with different input")
                return row[1], False
            db.execute("INSERT INTO requests VALUES (?,?,?,?,?)", (scope,key,digest,resource,dt.datetime.now(dt.timezone.utc).isoformat()))
        return resource, True
