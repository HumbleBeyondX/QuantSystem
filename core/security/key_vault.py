"""KeyVault — encrypted storage for API keys and secrets.

Encrypts secrets at rest using Fernet (AES-128-CBC + HMAC-SHA256).
Master key is derived from a user password via PBKDF2-HMAC-SHA256.

On Windows, optionally integrates with DPAPI for auto-unlock
(key stored in Windows Credential Manager, protected by user login).

Usage:
    vault = KeyVault(vault_path="data/vault.enc")
    vault.unlock("my_master_password")
    vault.store("binance_api_key", "abc123...")
    key = vault.retrieve("binance_api_key")
    vault.lock()

Requirements:
    pip install cryptography
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Default vault location
_DEFAULT_VAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "vault.enc"


class KeyVault:
    """Encrypted secret storage for API keys and credentials.

    All secrets are encrypted with Fernet (symmetric) using a key
    derived from the master password via PBKDF2.
    """

    def __init__(self, vault_path: Optional[Path] = None):
        self._vault_path = vault_path or _DEFAULT_VAULT_PATH
        self._fernet = None
        self._secrets: Dict[str, str] = {}
        self._salt: Optional[bytes] = None
        self._unlocked = False

    # ------------------------------------------------------------------
    # Key derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        """Derive a Fernet key from password + salt using PBKDF2."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,  # OWASP recommended minimum
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        return key

    # ------------------------------------------------------------------
    # Unlock / Lock
    # ------------------------------------------------------------------

    def unlock(self, password: str) -> None:
        """Unlock the vault with a master password.

        If the vault file doesn't exist, creates a new empty vault.
        """
        from cryptography.fernet import Fernet, InvalidToken

        if self._vault_path.exists():
            raw = self._vault_path.read_bytes()
            # First 16 bytes = salt, rest = encrypted data
            self._salt = raw[:16]
            encrypted = raw[16:]
            key = self._derive_key(password, self._salt)
            self._fernet = Fernet(key)
            try:
                decrypted = self._fernet.decrypt(encrypted)
                self._secrets = json.loads(decrypted.decode("utf-8"))
            except InvalidToken:
                self._fernet = None
                raise ValueError("Incorrect password or corrupted vault")
        else:
            # New vault
            self._salt = secrets.token_bytes(16)
            key = self._derive_key(password, self._salt)
            self._fernet = Fernet(key)
            self._secrets = {}
            self._save()

        self._unlocked = True
        logger.info("vault_unlocked", keys=len(self._secrets))

    def lock(self) -> None:
        """Lock the vault — clears secrets from memory."""
        self._fernet = None
        self._secrets = {}
        self._unlocked = False
        logger.info("vault_locked")

    @property
    def is_unlocked(self) -> bool:
        return self._unlocked

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def store(self, name: str, secret: str) -> None:
        """Store or update a secret."""
        self._require_unlocked()
        self._secrets[name] = secret
        self._save()
        logger.info("vault_secret_stored", name=name)

    def retrieve(self, name: str) -> Optional[str]:
        """Retrieve a secret by name. Returns None if not found."""
        self._require_unlocked()
        return self._secrets.get(name)

    def delete(self, name: str) -> bool:
        """Delete a secret. Returns True if it existed."""
        self._require_unlocked()
        if name in self._secrets:
            del self._secrets[name]
            self._save()
            logger.info("vault_secret_deleted", name=name)
            return True
        return False

    def list_keys(self) -> List[str]:
        """List all stored secret names (not the values)."""
        self._require_unlocked()
        return list(self._secrets.keys())

    def has(self, name: str) -> bool:
        """Check if a secret exists."""
        self._require_unlocked()
        return name in self._secrets

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Encrypt and save secrets to disk."""
        if not self._fernet or self._salt is None:
            raise RuntimeError("Vault not unlocked")

        self._vault_path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(self._secrets).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        self._vault_path.write_bytes(self._salt + encrypted)

    # ------------------------------------------------------------------
    # Windows DPAPI integration (optional)
    # ------------------------------------------------------------------

    def store_master_key_dpapi(self, password: str) -> None:
        """Store the master password in Windows Credential Manager via DPAPI.

        This allows the vault to auto-unlock without prompting for a password,
        protected by the Windows user login session.

        Only works on Windows. Silently skips on other platforms.
        """
        if os.name != "nt":
            logger.info("dpapi_skip_not_windows")
            return

        try:
            import ctypes
            from ctypes import wintypes

            # Use Windows Credential Manager (CredWrite)
            import subprocess
            # Store via cmdkey (simpler than raw Win32 API)
            subprocess.run(
                ["cmdkey", "/generic:QuantSystem_Vault", "/user:vault", f"/pass:{password}"],
                check=True,
                capture_output=True,
            )
            logger.info("dpapi_master_key_stored")
        except Exception as e:
            logger.warning("dpapi_store_failed", error=str(e))

    def try_auto_unlock_dpapi(self) -> bool:
        """Try to auto-unlock using DPAPI-stored master password.

        Returns True if successful, False otherwise.
        """
        if os.name != "nt":
            return False

        try:
            import subprocess
            result = subprocess.run(
                ["cmdkey", "/list:QuantSystem_Vault"],
                capture_output=True,
                text=True,
            )
            if "QuantSystem_Vault" not in result.stdout:
                return False

            # Extract password (Windows doesn't expose it via cmdkey /list,
            # but we can use it via CredRead Win32 API)
            # For now, this is a placeholder — full DPAPI integration
            # requires ctypes calls to CredReadW
            logger.debug("dpapi_auto_unlock_attempted")
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_unlocked(self) -> None:
        if not self._unlocked:
            raise RuntimeError("Vault is locked — call unlock() first")

    def change_password(self, old_password: str, new_password: str) -> None:
        """Change the master password."""
        # Verify old password by trying to decrypt
        self.lock()
        self.unlock(old_password)

        # Re-encrypt with new password
        self._salt = secrets.token_bytes(16)
        key = self._derive_key(new_password, self._salt)
        from cryptography.fernet import Fernet
        self._fernet = Fernet(key)
        self._save()
        logger.info("vault_password_changed")
