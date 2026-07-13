"""Security module for vault and attestation."""

from src.security.vault import (
    Attestation,
    EnvVault,
    FlySecretsVault,
    HashiCorpVault,
    Vault,
    VaultBackend,
    VaultConfig,
    VaultFactory,
    create_attestation,
    get_secret,
    set_secret,
    verify_live_trading_allowed,
)

__all__ = [
    "Attestation",
    "EnvVault",
    "FlySecretsVault",
    "HashiCorpVault",
    "Vault",
    "VaultBackend",
    "VaultConfig",
    "VaultFactory",
    "create_attestation",
    "get_secret",
    "set_secret",
    "verify_live_trading_allowed",
]