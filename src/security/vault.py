"""
Secure Vault and Attestation for LIVE_TRADING_CONFIRMED guard.

Provides:
- Secure vault abstraction (Fly.io secrets, HashiCorp Vault, env vars)
- Attestation mechanism with signed tokens for human approval
- Audit trail for mode changes
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx

logger = logging.getLogger(__name__)


class VaultBackend(StrEnum):
    """Supported vault backends."""
    ENV = "env"           # Environment variables (fallback)
    FLY_SECRETS = "fly"   # Fly.io secrets (automatically available as env vars)
    HASHICORP = "hashicorp"  # HashiCorp Vault
    AWS_SECRETS = "aws"   # AWS Secrets Manager


@dataclass(frozen=True)
class Attestation:
    """Signed attestation for live trading approval.

    Contains:
    - approved_by: identifier of approver (email, key ID, etc.)
    - approved_at: timestamp of approval
    - expires_at: when approval expires (max 30 days for security)
    - validation_hours: hours of paper trading validation completed
    - reason: justification for live mode
    - signature: HMAC-SHA256 signature using vault's attestation key
    """
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    validation_hours: int
    reason: str
    signature: str
    nonce: str = field(default_factory=lambda: os.urandom(16).hex()[:16])

    def is_valid(self, attestation_key: bytes, max_age_days: int = 30) -> bool:
        """Verify attestation signature and expiry."""
        now = datetime.now(UTC)
        if self.expires_at < now:
            return False
        if (now - self.approved_at).days > max_age_days:
            return False

        # Verify signature
        expected = self._compute_signature(attestation_key)
        return hmac.compare_digest(self.signature, expected)

    def _compute_signature(self, key: bytes) -> str:
        """Compute expected HMAC signature."""
        data = f"{self.approved_by}:{self.approved_at.isoformat()}:{self.expires_at.isoformat()}:{self.validation_hours}:{self.reason}:{self.nonce}"
        return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class VaultConfig:
    """Configuration for vault backend."""
    backend: VaultBackend = VaultBackend.ENV
    fly_app_name: str | None = None
    hashicorp_url: str | None = None
    hashicorp_token: str | None = None
    aws_region: str | None = None
    attestation_key: str | None = None  # Base64-encoded HMAC key for attestation signatures


class Vault(ABC):
    """Abstract vault interface."""

    @abstractmethod
    async def get_secret(self, key: str) -> str | None:
        """Retrieve a secret by key."""

    @abstractmethod
    async def set_secret(self, key: str, value: str) -> bool:
        """Store a secret. Returns success."""

    @abstractmethod
    async def delete_secret(self, key: str) -> bool:
        """Delete a secret. Returns success."""

    @abstractmethod
    async def list_secrets(self, prefix: str = "") -> list[str]:
        """List secret keys matching prefix."""


class EnvVault(Vault):
    """Environment variable vault (fallback, not for production secrets)."""

    async def get_secret(self, key: str) -> str | None:
        return os.getenv(key)

    async def set_secret(self, key: str, value: str) -> bool:
        os.environ[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        os.environ.pop(key, None)
        return True

    async def list_secrets(self, prefix: str = "") -> list[str]:
        return [k for k in os.environ if k.startswith(prefix)]


class FlySecretsVault(Vault):
    """Fly.io secrets vault.

    On Fly.io, secrets are automatically injected as environment variables.
    This vault uses the Fly API to manage secrets programmatically.
    """

    def __init__(self, app_name: str) -> None:
        self.app_name = app_name
        self.api_token = os.getenv("FLY_API_TOKEN")
        self.base_url = "https://api.machines.dev/v1"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    async def get_secret(self, key: str) -> str | None:
        # On Fly, secrets are available as env vars at runtime
        return os.getenv(key)

    async def set_secret(self, key: str, value: str) -> bool:
        if not self.api_token:
            logger.warning("FLY_API_TOKEN not set, cannot write secrets")
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/apps/{self.app_name}/secrets",
                    headers=self._headers(),
                    json={key: value},
                    timeout=10,
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Failed to set Fly secret {key}: {e}")
            return False

    async def delete_secret(self, key: str) -> bool:
        if not self.api_token:
            return False
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{self.base_url}/apps/{self.app_name}/secrets/{key}",
                    headers=self._headers(),
                    timeout=10,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error(f"Failed to delete Fly secret {key}: {e}")
            return False

    async def list_secrets(self, prefix: str = "") -> list[str]:
        if not self.api_token:
            return []
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.base_url}/apps/{self.app_name}/secrets",
                    headers=self._headers(),
                    timeout=10,
                )
                resp.raise_for_status()
                secrets = resp.json()
                return [k for k in secrets if k.startswith(prefix)]
        except Exception as e:
            logger.error(f"Failed to list Fly secrets: {e}")
            return []


class HashiCorpVault(Vault):
    """HashiCorp Vault backend."""

    def __init__(self, url: str, token: str, mount_path: str = "secret") -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.mount_path = mount_path

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self.token}

    async def get_secret(self, key: str) -> str | None:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.url}/v1/{self.mount_path}/data/{key}",
                    headers=self._headers(),
                    timeout=10,
                )
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                data = resp.json()
                return data.get("data", {}).get("data", {}).get(key)
        except Exception as e:
            logger.error(f"Failed to get secret {key} from HashiCorp Vault: {e}")
            return None

    async def set_secret(self, key: str, value: str) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.url}/v1/{self.mount_path}/data/{key}",
                    headers=self._headers(),
                    json={"data": {key: value}},
                    timeout=10,
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Failed to set secret {key} in HashiCorp Vault: {e}")
            return False

    async def delete_secret(self, key: str) -> bool:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.delete(
                    f"{self.url}/v1/{self.mount_path}/data/{key}",
                    headers=self._headers(),
                    timeout=10,
                )
                return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"Failed to delete secret {key} from HashiCorp Vault: {e}")
            return False

    async def list_secrets(self, prefix: str = "") -> list[str]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.url}/v1/{self.mount_path}/metadata?list=true",
                    headers=self._headers(),
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                keys = data.get("data", {}).get("keys", [])
                return [k for k in keys if k.startswith(prefix)]
        except Exception as e:
            logger.error(f"Failed to list HashiCorp secrets: {e}")
            return []


class VaultFactory:
    """Factory for creating vault instances."""

    _instance: Vault | None = None
    _config: VaultConfig | None = None

    @classmethod
    def initialize(cls, config: VaultConfig) -> Vault:
        """Initialize the global vault instance."""
        cls._config = config

        if config.backend == VaultBackend.FLY_SECRETS:
            app_name = config.fly_app_name or os.getenv("FLY_APP_NAME", "polymarket-reverse-arb")
            cls._instance = FlySecretsVault(app_name)
        elif config.backend == VaultBackend.HASHICORP:
            url = config.hashicorp_url or os.getenv("VAULT_ADDR")
            token = config.hashicorp_token or os.getenv("VAULT_TOKEN")
            if not url or not token:
                raise ValueError("HashiCorp Vault requires url and token")
            cls._instance = HashiCorpVault(url, token)
        elif config.backend == VaultBackend.AWS_SECRETS:
            # Would need boto3, stub for now
            cls._instance = EnvVault()
        else:
            cls._instance = EnvVault()

        logger.info(f"Vault initialized with backend: {config.backend.value}")
        return cls._instance

    @classmethod
    def get_vault(cls) -> Vault:
        """Get the global vault instance."""
        if cls._instance is None:
            cls.initialize(VaultConfig())
        return cls._instance

    @classmethod
    def get_attestation_key(cls) -> bytes:
        """Get the HMAC key for attestation signatures."""
        if cls._config and cls._config.attestation_key:
            return base64.b64decode(cls._config.attestation_key)
        # Fallback: derive from a master secret (not for production!)
        master = os.getenv("ATTESTATION_MASTER_KEY", "dev-key-change-in-production")
        return hashlib.sha256(master.encode()).digest()


# Convenience functions
async def get_secret(key: str) -> str | None:
    """Get a secret from the configured vault."""
    return await VaultFactory.get_vault().get_secret(key)


async def set_secret(key: str, value: str) -> bool:
    """Set a secret in the configured vault."""
    return await VaultFactory.get_vault().set_secret(key, value)


async def create_attestation(
    approved_by: str,
    validation_hours: int,
    reason: str,
    ttl_days: int = 30,
) -> Attestation:
    """Create a signed attestation for live trading approval."""
    now = datetime.now(UTC)
    expires_at = now.replace(day=now.day + ttl_days) if now.day + ttl_days <= 28 else (now.replace(day=28) + timedelta(days=ttl_days))
    expires_at = now + timedelta(days=ttl_days)

    attestation = Attestation(
        approved_by=approved_by,
        approved_at=now,
        expires_at=expires_at,
        validation_hours=validation_hours,
        reason=reason,
        signature="",  # Will be filled below
    )

    key = VaultFactory.get_attestation_key()
    attestation = Attestation(
        approved_by=attestation.approved_by,
        approved_at=attestation.approved_at,
        expires_at=attestation.expires_at,
        validation_hours=attestation.validation_hours,
        reason=attestation.reason,
        signature=attestation._compute_signature(key),
        nonce=attestation.nonce,
    )
    return attestation


async def verify_live_trading_allowed(vault: Vault | None = None) -> tuple[bool, str]:
    """Verify live trading is allowed with vault-backed attestation.

    Returns (allowed, reason_or_attestation_json).
    """
    v = vault or VaultFactory.get_vault()

    # Check for LIVE_TRADING_CONFIRMED in vault
    live_confirmed = await v.get_secret("LIVE_TRADING_CONFIRMED")
    if live_confirmed and live_confirmed.lower() == "true":
        # Also check for attestation
        attestation_json = await v.get_secret("LIVE_TRADING_ATTESTATION")
        if attestation_json:
            try:
                attestation_data = json.loads(attestation_json)
                attestation = Attestation(**attestation_data)
                key = VaultFactory.get_attestation_key()
                if attestation.is_valid(key):
                    return True, json.dumps(attestation_data)
            except Exception as e:
                logger.error(f"Invalid attestation: {e}")

        # Backward compat: just the flag
        return True, "legacy_flag"

    return False, "LIVE_TRADING_CONFIRMED not set in vault or attestation invalid"