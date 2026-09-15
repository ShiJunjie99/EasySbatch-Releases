import json

import pytest

from sbatch_agent.cluster_profile import ClusterProfile
from sbatch_agent.credential_store import CredentialBackend
from sbatch_agent.ssh_credential_store import (
    ACCOUNT_NAME, SERVICE_NAME, KeyringSSHCredentialStore,
    SSHCredentialStoreError,
)


class WindowsVault:
    __module__ = "keyring.backends.Windows"

    def __init__(self):
        self.value = None

    def get_password(self, service, account):
        assert (service, account) == (SERVICE_NAME, ACCOUNT_NAME)
        return self.value

    def set_password(self, service, account, value):
        assert (service, account) == (SERVICE_NAME, ACCOUNT_NAME)
        self.value = value

    def delete_password(self, service, account):
        assert (service, account) == (SERVICE_NAME, ACCOUNT_NAME)
        self.value = None


def profile(host="10.158.132.77", port=3088):
    return ClusterProfile.from_mapping({
        "id": "primary", "display_name": "Synthetic",
        "host": host, "ssh_port": port,
    })


def test_native_vault_binds_password_to_exact_endpoint_and_username():
    backend = WindowsVault()
    store = KeyringSSHCredentialStore(
        backend=backend, platform="win32", os_name="nt",
    )
    assert store.backend == CredentialBackend.WINDOWS_CREDENTIAL_MANAGER
    assert store.get(profile(), "student") is None

    assert store.set(profile(), "student", "synthetic-password") == (
        CredentialBackend.WINDOWS_CREDENTIAL_MANAGER
    )
    payload = json.loads(backend.value)
    assert payload["password"] == "synthetic-password"
    assert store.get(profile(), "student").get_secret_value() == "synthetic-password"
    assert store.get(profile(port=22), "student") is None
    assert store.get(profile(), "someone_else") is None
    assert store.delete() is True
    assert store.get(profile(), "student") is None


def test_native_vault_rejects_unapproved_backend_and_invalid_payload():
    class PlaintextBackend(WindowsVault):
        __module__ = "keyrings.alt.file"

    unavailable = KeyringSSHCredentialStore(
        backend=PlaintextBackend(), platform="win32", os_name="nt",
    )
    assert unavailable.available is False
    with pytest.raises(SSHCredentialStoreError):
        unavailable.set(profile(), "student", "synthetic-password")

    backend = WindowsVault()
    store = KeyringSSHCredentialStore(
        backend=backend, platform="win32", os_name="nt",
    )
    backend.value = '{"password":"not-bound"}'
    with pytest.raises(SSHCredentialStoreError):
        store.get(profile(), "student")
