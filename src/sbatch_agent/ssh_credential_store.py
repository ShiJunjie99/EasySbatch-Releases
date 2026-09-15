"""Operating-system credential storage for the desktop SSH account.

Only the password is secret. The selected cluster endpoint and Linux username
remain in the product-owned ``cluster.json`` so the UI can show the last account
without opening the system vault. The vault payload repeats that identity and
is rejected unless it still matches the active cluster configuration.
"""

from __future__ import annotations

import json

from pydantic import SecretStr

from .cluster_profile import ClusterProfile
from .credential_store import CredentialBackend, _backend_kind
from .launcher_client import validate_username


SERVICE_NAME = "Beta EasySbatch SSH"
ACCOUNT_NAME = "primary"
SCHEMA_VERSION = 1


class SSHCredentialStoreError(RuntimeError):
    """A fixed, non-secret failure returned when the native vault is unusable."""

    def __init__(self):
        super().__init__("SSH_CREDENTIAL_UNAVAILABLE")


def validate_ssh_password(value: object) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 1024
        or any(character in value for character in ("\n", "\r", "\x00"))
    ):
        raise SSHCredentialStoreError()
    return value


class KeyringSSHCredentialStore:
    """Store one bound desktop SSH credential in an approved native keyring."""

    def __init__(self, *, backend=None, keyring_module=None, platform=None, os_name=None):
        self._backend_object = backend
        if self._backend_object is None:
            try:
                if keyring_module is None:
                    import keyring as keyring_module  # type: ignore
                self._backend_object = keyring_module.get_keyring()
            except Exception:
                self._backend_object = None
        self._backend = (
            _backend_kind(self._backend_object, platform=platform, os_name=os_name)
            if self._backend_object is not None else CredentialBackend.UNAVAILABLE
        )

    @property
    def backend(self) -> CredentialBackend:
        return self._backend

    @property
    def available(self) -> bool:
        return self._backend != CredentialBackend.UNAVAILABLE

    def _require(self):
        if not self.available:
            raise SSHCredentialStoreError()
        return self._backend_object

    def get(self, profile: ClusterProfile, username: str) -> SecretStr | None:
        username = validate_username(username)
        try:
            raw = self._require().get_password(SERVICE_NAME, ACCOUNT_NAME)
        except SSHCredentialStoreError:
            raise
        except Exception:
            raise SSHCredentialStoreError() from None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "schema_version", "host", "ssh_port", "username", "password",
            }:
                raise ValueError
            password = validate_ssh_password(value["password"])
        except (TypeError, ValueError, json.JSONDecodeError, SSHCredentialStoreError):
            raise SSHCredentialStoreError() from None
        if (
            value["schema_version"] != SCHEMA_VERSION
            or value["host"] != profile.host
            or value["ssh_port"] != profile.ssh_port
            or value["username"] != username
        ):
            return None
        return SecretStr(password)

    def set(self, profile: ClusterProfile, username: str, password: str) -> CredentialBackend:
        username = validate_username(username)
        password = validate_ssh_password(password)
        payload = json.dumps({
            "schema_version": SCHEMA_VERSION,
            "host": profile.host,
            "ssh_port": profile.ssh_port,
            "username": username,
            "password": password,
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            self._require().set_password(SERVICE_NAME, ACCOUNT_NAME, payload)
        except SSHCredentialStoreError:
            raise
        except Exception:
            raise SSHCredentialStoreError() from None
        return self.backend

    def delete(self) -> bool:
        backend = self._require()
        try:
            if backend.get_password(SERVICE_NAME, ACCOUNT_NAME) is None:
                return False
            backend.delete_password(SERVICE_NAME, ACCOUNT_NAME)
            return True
        except SSHCredentialStoreError:
            raise
        except Exception:
            raise SSHCredentialStoreError() from None


def create_ssh_credential_store() -> KeyringSSHCredentialStore:
    return KeyringSSHCredentialStore()
