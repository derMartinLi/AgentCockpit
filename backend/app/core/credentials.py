from __future__ import annotations

import keyring
import keyring.errors as keyring_errors

from backend.app.core.config import Settings


class CredentialStore:
    SERVICE_NAME = "agent-cockpit"
    USERNAME = "provider-api-key"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def set_api_key(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("API Key cannot be empty")
        keyring.set_password(self.SERVICE_NAME, self.USERNAME, api_key)

    def get_api_key(self) -> str | None:
        if self.settings.api_key:
            return self.settings.api_key
        try:
            return keyring.get_password(self.SERVICE_NAME, self.USERNAME)
        except keyring_errors.KeyringError:
            return None

    def has_api_key(self) -> bool:
        return self.get_api_key() is not None

    def redact(self, value: str) -> str:
        secret = self.get_api_key()
        return value.replace(secret, "[REDACTED]") if secret else value
