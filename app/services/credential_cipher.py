import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class CredentialDecryptionError(RuntimeError):
    pass


class CredentialCipher:
    """Encrypt credentials at rest with a key derived from the server session secret."""

    PREFIX = "fernet-v1:"

    def __init__(self, secret: str | None = None) -> None:
        source = secret if secret is not None else get_settings().dashboard_session_secret
        if not source:
            raise ValueError("A server secret is required to encrypt calendar credentials.")
        derived = hashlib.sha256(f"calendar-credentials-v1:{source}".encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, value: str) -> str:
        if not value:
            raise ValueError("A non-empty credential is required.")
        token = self._fernet.encrypt(value.encode()).decode()
        return f"{self.PREFIX}{token}"

    def decrypt(self, value: str | None) -> str:
        if not value or not value.startswith(self.PREFIX):
            raise CredentialDecryptionError("The stored calendar credential is not encrypted.")
        try:
            return self._fernet.decrypt(value.removeprefix(self.PREFIX).encode()).decode()
        except InvalidToken as exc:
            raise CredentialDecryptionError(
                "The stored calendar credential could not be decrypted."
            ) from exc
