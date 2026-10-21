import sys
from types import SimpleNamespace

import pytest

from productivity import state as storage
from productivity.common import Problem


def test_native_state_locations(tmp_path):
    home = tmp_path / "user"
    local = tmp_path / "local"
    xdg = tmp_path / "xdg"
    env = {"LOCALAPPDATA": str(local), "XDG_DATA_HOME": str(xdg)}
    assert storage.default_state_root(platform="win32", environ=env, home=home) == local / "HarnessProductivity"
    assert storage.default_state_root(platform="darwin", environ=env, home=home) == home / "Library/Application Support/HarnessProductivity"
    assert storage.default_state_root(platform="linux", environ=env, home=home) == xdg / "HarnessProductivity"
    assert storage.default_state_root(platform="win32", environ={}, home=home) == home / "AppData/Local/HarnessProductivity"
    for env in ({}, {"XDG_DATA_HOME": "relative/data"}):
        assert storage.default_state_root(platform="linux", environ=env, home=home) == home / ".local/share/HarnessProductivity"


def test_keyring_calls_explicit_backend_and_scopes_profiles(monkeypatch, tmp_path):
    import keyring
    from keyring.errors import PasswordDeleteError
    stored = {}

    class Backend:
        def get_password(self, service, user):
            return stored.get((service, user))

        def set_password(self, service, user, value):
            stored[service, user] = value

        def delete_password(self, service, user):
            if stored.pop((service, user), None) is None:
                raise PasswordDeleteError("missing")

    monkeypatch.setattr(storage, "_secure_keyring", lambda: Backend())
    monkeypatch.setattr(keyring, "get_keyring", lambda: pytest.fail("No chainer or plaintext fallback"))
    a, b = tmp_path / "a", tmp_path / "b"
    storage._keyring_operation("set", a, "token", "synthetic-secret")
    assert storage._keyring_operation("get", a, "token") == "synthetic-secret"
    assert storage._keyring_operation("get", b, "token") is None
    storage._keyring_operation("delete", a, "token")
    storage._keyring_operation("delete", a, "token")
    assert storage._keyring_operation("get", a, "token") is None


def test_unavailable_keyring_is_actionable_and_redacted(monkeypatch, tmp_path):
    def broken():
        raise RuntimeError("provider details with synthetic-secret")

    monkeypatch.setattr(storage, "_secure_keyring", broken)
    with pytest.raises(Problem) as error:
        storage._keyring_operation("get", tmp_path, "token")
    assert error.value.code == "credential_store_unavailable"
    assert "ENVIRONMENT.md" in str(error.value)
    assert "synthetic-secret" not in str(error.value)


def test_linux_locked_collection_and_item_do_not_prompt(monkeypatch):
    from keyring.backends import SecretService
    from keyring.errors import KeyringLocked

    class Backend:
        priority = 5

    closed = []
    collection = SimpleNamespace(is_locked=lambda: True)
    connection = SimpleNamespace(close=lambda: closed.append(True))
    service = SimpleNamespace(dbus_init=lambda: connection,
                              get_default_collection=lambda conn: collection)
    monkeypatch.setattr(SecretService, "Keyring", Backend)
    monkeypatch.setitem(sys.modules, "secretstorage", service)
    monkeypatch.setattr(sys, "platform", "linux")
    backend = storage._secure_keyring()
    with pytest.raises(KeyringLocked):
        backend.get_preferred_collection()
    assert closed == [True]
    with pytest.raises(KeyringLocked):
        backend.unlock(collection)
    collection.is_locked = lambda: False
    assert backend.get_preferred_collection() is collection
    backend.unlock(collection)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_posix_profile_and_config_permissions(tmp_path):
    import stat
    profile = storage.State(tmp_path)
    profile.save("calendar", {"provider": "test"})
    assert stat.S_IMODE(profile.path.stat().st_mode) == 0o700
    assert stat.S_IMODE((profile.path / "calendar.json").stat().st_mode) == 0o600
