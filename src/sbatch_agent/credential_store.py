"""Per-local-user AI credential storage with no plaintext-file fallback.

The public identifier is stable, while the operating-system account boundary is
provided by the credential backend itself.  Secret values have an intentionally
redacted string/repr and must be revealed explicitly at the HTTPS call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import os
import sys
import threading
from typing import Protocol


SERVICE_NAME = "EasySbatch"
PROVIDER_NAME = "deepseek"
PROFILE_NAME = "default"
ACCOUNT_NAME = f"{PROVIDER_NAME}:{PROFILE_NAME}"


class CredentialBackend(StrEnum):
    WINDOWS_CREDENTIAL_MANAGER = "Windows Credential Manager"
    MACOS_KEYCHAIN = "macOS Keychain"
    SECRET_SERVICE = "Secret Service"
    KWALLET = "KWallet"
    SESSION_ONLY = "Session only"
    UNAVAILABLE = "Unavailable"


class CredentialStoreError(RuntimeError):
    """Safe fixed-category error; backend exception text is never retained."""

    def __init__(self, code="AI_LOCAL_CREDENTIAL_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class SecretValue:
    _value: str = field(repr=False)

    def __post_init__(self):
        validate_api_key(self._value)

    def reveal(self):
        return self._value

    def __repr__(self):
        return "SecretValue(<redacted>)"

    def __str__(self):
        return "<redacted>"


def validate_api_key(value):
    if (not isinstance(value, str) or not 8 <= len(value) <= 512 or
            not value.isascii() or not value.isprintable() or value.strip() != value or
            "\x00" in value):
        raise CredentialStoreError("AI_CREDENTIAL_INVALID")
    return value


class AISecretStore(Protocol):
    @property
    def backend(self) -> CredentialBackend: ...
    @property
    def available(self) -> bool: ...
    def get(self) -> SecretValue | None: ...
    def set(self, value: str) -> None: ...
    def delete(self) -> bool: ...
    def exists(self) -> bool: ...


def _backend_kind(backend, *, platform=None, os_name=None):
    platform = sys.platform if platform is None else platform
    os_name = os.name if os_name is None else os_name
    name = (backend.__class__.__module__ + "." + backend.__class__.__name__).lower()
    if os_name == "nt" and any(value in name for value in ("windows", "winvault")):
        return CredentialBackend.WINDOWS_CREDENTIAL_MANAGER
    if platform == "darwin" and any(value in name for value in ("macos", "keychain")):
        return CredentialBackend.MACOS_KEYCHAIN
    if platform.startswith("linux") and any(value in name for value in ("secretservice", "libsecret")):
        return CredentialBackend.SECRET_SERVICE
    if platform.startswith("linux") and any(value in name for value in ("kwallet", "kdewallet")):
        return CredentialBackend.KWALLET
    # In particular, reject keyring.backends.fail/null and keyrings.alt files.
    return CredentialBackend.UNAVAILABLE


class KeyringAISecretStore:
    """Adapter for approved native keyring backends only."""

    def __init__(self, *, backend=None, keyring_module=None, platform=None, os_name=None):
        self._backend_object = backend
        if self._backend_object is None:
            try:
                if keyring_module is None:
                    import keyring as keyring_module  # type: ignore
                self._backend_object = keyring_module.get_keyring()
            except Exception:
                self._backend_object = None
        self._backend = (_backend_kind(self._backend_object, platform=platform, os_name=os_name)
                         if self._backend_object is not None else CredentialBackend.UNAVAILABLE)

    @property
    def backend(self):
        return self._backend

    @property
    def available(self):
        return self._backend != CredentialBackend.UNAVAILABLE

    def _require(self):
        if not self.available:
            raise CredentialStoreError()
        return self._backend_object

    def get(self):
        try:
            value = self._require().get_password(SERVICE_NAME, ACCOUNT_NAME)
            return None if value is None else SecretValue(value)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError() from None

    def set(self, value):
        validate_api_key(value)
        try:
            self._require().set_password(SERVICE_NAME, ACCOUNT_NAME, value)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError() from None

    def delete(self):
        backend = self._require()
        try:
            if backend.get_password(SERVICE_NAME, ACCOUNT_NAME) is None:
                return False
            backend.delete_password(SERVICE_NAME, ACCOUNT_NAME)
            return True
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError() from None

    def exists(self):
        return self.get() is not None

    def __repr__(self):
        return f"KeyringAISecretStore(backend={self.backend.value!r}, available={self.available!r})"


class SessionAISecretStore:
    """Memory-only fallback owned by the persistent local Agent process."""

    def __init__(self):
        self._secret = None
        self._lock = threading.RLock()

    @property
    def backend(self):
        return CredentialBackend.SESSION_ONLY

    @property
    def available(self):
        return True

    def get(self):
        with self._lock:
            return None if self._secret is None else SecretValue(self._secret)

    def set(self, value):
        validate_api_key(value)
        with self._lock:
            self._secret = value

    def delete(self):
        with self._lock:
            existed = self._secret is not None
            self._secret = None
            return existed

    def exists(self):
        with self._lock:
            return self._secret is not None

    def __repr__(self):
        return f"SessionAISecretStore(configured={self.exists()!r})"


class AICredentialManager:
    """Prefer the OS vault and expose session memory only when explicitly used."""

    def __init__(self, *, secure_store=None, session_store=None):
        self.secure_store = secure_store or KeyringAISecretStore()
        self.session_store = session_store or SessionAISecretStore()
        self._session_preferred = False
        self._preference_lock = threading.Lock()

    @property
    def backend(self):
        with self._preference_lock:
            session_preferred = self._session_preferred
        if session_preferred and self.session_store.exists():
            return CredentialBackend.SESSION_ONLY
        if self.secure_store.available:
            return self.secure_store.backend
        if self.session_store.exists():
            return CredentialBackend.SESSION_ONLY
        return CredentialBackend.UNAVAILABLE

    @property
    def secure_available(self):
        return self.secure_store.available

    def get(self):
        with self._preference_lock:
            session_preferred = self._session_preferred
        if session_preferred:
            return self.session_store.get()
        if self.secure_store.available:
            return self.secure_store.get()
        return self.session_store.get()

    def set(self, value, *, session_only=False):
        if session_only:
            self.session_store.set(value)
            with self._preference_lock:
                self._session_preferred = True
            return CredentialBackend.SESSION_ONLY
        if not self.secure_store.available:
            raise CredentialStoreError()
        self.secure_store.set(value)
        self.session_store.delete()
        with self._preference_lock:
            self._session_preferred = False
        return self.secure_store.backend

    def delete(self):
        deleted = self.session_store.delete()
        with self._preference_lock:
            self._session_preferred = False
        if self.secure_store.available:
            deleted = self.secure_store.delete() or deleted
        return deleted

    def exists(self):
        try:
            return self.get() is not None
        except CredentialStoreError:
            return False

    def status(self):
        try:
            configured = self.exists()
        except CredentialStoreError:
            configured = False
        return {"provider": PROVIDER_NAME, "profile": PROFILE_NAME,
                "configured": configured, "backend": self.backend.value}

    def __repr__(self):
        return (f"AICredentialManager(backend={self.backend.value!r}, "
                f"configured={self.exists()!r})")


_default_manager = None
_default_manager_lock = threading.Lock()


def create_ai_credential_manager():
    """Return the process-local manager (secure vault remains OS-scoped)."""
    global _default_manager
    with _default_manager_lock:
        if _default_manager is None:
            _default_manager = AICredentialManager()
        return _default_manager
