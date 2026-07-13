"""
API Authentication and Authorization for Reverse Arbitrage Bot.

Provides:
- API key authentication via header or Bearer token
- JWT token authentication (for dashboard sessions)
- Role-based access control (admin vs read-only)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from src.core.config import get_settings

logger = logging.getLogger(__name__)

# Security schemes
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


class Role(StrEnum):
    """User roles for RBAC."""
    ADMIN = "admin"
    READONLY = "readonly"


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request context."""
    api_key: str
    role: Role
    client_id: str
    authenticated_at: float


# Global auth state
_valid_api_keys: dict[str, Role] = {}
_admin_keys: set[str] = set()


def init_auth() -> None:
    """Initialize auth from settings."""
    global _valid_api_keys, _admin_keys
    settings = get_settings()

    if not settings.require_api_auth:
        logger.warning("API authentication is DISABLED - all endpoints accessible without auth")
        return

    # Admin keys (can start/stop engine, access all endpoints)
    if settings.api_auth_token:
        _valid_api_keys[settings.api_auth_token] = Role.ADMIN
        _admin_keys.add(settings.api_auth_token)
        logger.info("Admin API key configured")

    # Read-only keys from environment (comma-separated)
    readonly_keys = settings.api_readonly_keys
    if readonly_keys:
        for key in readonly_keys.split(","):
            key = key.strip()
            if key:
                _valid_api_keys[key] = Role.READONLY
                logger.info("Read-only API key configured")

    if not _valid_api_keys:
        logger.warning("No API keys configured but auth is required - all requests will be rejected")


def get_api_key_role(api_key: str) -> Role | None:
    """Get role for an API key."""
    return _valid_api_keys.get(api_key)


async def verify_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
    authorization: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
) -> AuthContext:
    """Verify API key from header or Bearer token."""
    settings = get_settings()

    if not settings.require_api_auth:
        # Return anonymous context when auth disabled
        return AuthContext(
            api_key="anonymous",
            role=Role.ADMIN,
            client_id="anonymous",
            authenticated_at=time.time(),
        )

    # Try X-API-Key header first
    key = api_key
    source = "header"

    # Fall back to Authorization: Bearer <key>
    if not key and authorization and authorization.scheme.lower() == "bearer":
        key = authorization.credentials
        source = "bearer"

    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide X-API-Key header or Authorization: Bearer <key>",
            headers={"WWW-Authenticate": "APIKey"},
        )

    role = _valid_api_keys.get(key)
    if not role:
        # Log without exposing the key
        key_preview = f"{key[:8]}..." if len(key) > 8 else "***"
        logger.warning(f"Invalid API key attempted: {key_preview} (source: {source})")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "APIKey"},
        )

    # Generate a stable client ID from the key (without exposing full key)
    client_id = key[:8] + "..." + key[-4:] if len(key) > 12 else "***"

    return AuthContext(
        api_key=key,
        role=role,
        client_id=client_id,
        authenticated_at=time.time(),
    )


def require_admin(auth: AuthContext = Depends(verify_api_key)) -> AuthContext:
    """Dependency that requires admin role."""
    if auth.role != Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required for this endpoint",
        )
    return auth


def require_readonly_or_admin(auth: AuthContext = Depends(verify_api_key)) -> AuthContext:
    """Dependency that allows readonly or admin role."""
    if auth.role not in (Role.READONLY, Role.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return auth


# Alias for backward compatibility
verify_auth = verify_api_key

# Dependency aliases for backward compatibility with tests
AdminDependency = Depends(require_admin)
ReadOnlyDependency = Depends(require_readonly_or_admin)
AuthDependency = Depends(verify_api_key)