import hashlib
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_repository import AuthRepository
from errors import AppError, ErrorCode
from rag_errors import RagError
from security_settings import is_production

logger = logging.getLogger(__name__)

ROLE_ORDER = {
    "viewer": 10,
    "officer": 20,
    "knowledge_manager": 30,
    "admin": 40,
}

BOOTSTRAP_ADMIN_ID = "00000000-0000-0000-0000-000000000001"
BOOTSTRAP_ADMIN_EMAIL = "bootstrap-admin@local"
JWT_ALGORITHM = "HS256"
JWT_ISSUER = "forest-rag-api"
REFRESH_TOKEN_BYTES = 32

bearer_scheme = HTTPBearer(auto_error=False)
password_hasher = PasswordHasher()


class AuthError(AppError):
    def __init__(self, message: str, status_code: int = status.HTTP_401_UNAUTHORIZED):
        super().__init__(message, code=ErrorCode.AUTH_ERROR, status_code=status_code)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    role: str
    full_name: str | None = None
    must_change_password: bool = False
    is_bootstrap: bool = False
    token_version: int = field(default=0, repr=False)


def _jwt_secret() -> str:
    return os.getenv("JWT_SECRET_KEY", "").strip()


def _jwt_expires_minutes() -> int:
    raw = os.getenv("JWT_EXPIRES_MINUTES", str(60 * 24)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RagError("JWT_EXPIRES_MINUTES must be an integer") from exc
    if value <= 0:
        raise RagError("JWT_EXPIRES_MINUTES must be greater than 0")
    return value


def _refresh_expires_days() -> int:
    raw = os.getenv("REFRESH_TOKEN_EXPIRES_DAYS", "30").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RagError("REFRESH_TOKEN_EXPIRES_DAYS must be an integer") from exc
    if value <= 0:
        raise RagError("REFRESH_TOKEN_EXPIRES_DAYS must be greater than 0")
    return value


def _bootstrap_admin_token() -> str:
    return "" if is_production() else os.getenv("BOOTSTRAP_ADMIN_TOKEN", "").strip()


def _auth_disabled() -> bool:
    return not is_production() and os.getenv("AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes"}


def _user_from_row(row: dict) -> CurrentUser:
    if not row.get("is_active"):
        # A disabled account is no longer valid authentication, even if its JWT
        # has not expired yet.
        raise AuthError("User is inactive", status.HTTP_401_UNAUTHORIZED)

    return CurrentUser(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        full_name=row.get("full_name"),
        must_change_password=bool(row.get("must_change_password", False)),
        token_version=int(row.get("token_version", 0)),
    )


def public_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "full_name": row.get("full_name"),
        "role": row["role"],
        "is_active": row.get("is_active", True),
        "must_change_password": bool(row.get("must_change_password", False)),
        "last_login_at": row.get("last_login_at"),
        "metadata": row.get("metadata") or {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def create_access_token(user: CurrentUser) -> tuple[str, str]:
    secret = _jwt_secret()
    if not secret:
        raise RagError("JWT_SECRET_KEY is not configured")

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=_jwt_expires_minutes())
    payload = {
        "iss": JWT_ISSUER,
        "sub": user.id,
        "email": user.email,
        "role": user.role,
        "full_name": user.full_name,
        "must_change_password": user.must_change_password,
        "ver": user.token_version,
        "iat": now,
        "exp": expires_at,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM), expires_at.isoformat()


def _user_from_jwt(token: str) -> CurrentUser:
    secret = _jwt_secret()
    if not secret:
        raise AuthError("JWT auth is not configured")

    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("JWT has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("Invalid JWT") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise AuthError("Invalid JWT")

    try:
        result = (
            AuthRepository().get_user_by_id(user_id)
        )
    except Exception as exc:
        raise AuthError("Could not validate credentials.") from exc

    if not result:
        raise AuthError("Invalid JWT")

    # Pre-migration JWTs have version zero, so deployment does not log everyone out.
    version = claims.get("ver", 0)
    if type(version) is not int or version != int(result.get("token_version", 0)):
        raise AuthError("Session has been revoked. Please log in again.")
    return _user_from_row(result)


def _user_from_token(token: str) -> CurrentUser:
    bootstrap_token = _bootstrap_admin_token()
    if bootstrap_token and secrets.compare_digest(token, bootstrap_token):
        return CurrentUser(
            id=BOOTSTRAP_ADMIN_ID,
            email=BOOTSTRAP_ADMIN_EMAIL,
            role="admin",
            full_name="Bootstrap Admin",
            must_change_password=False,
            is_bootstrap=True,
        )

    return _user_from_jwt(token)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
) -> CurrentUser:
    if _auth_disabled():
        return CurrentUser(
            id=BOOTSTRAP_ADMIN_ID,
            email="auth-disabled@local",
            role="admin",
            full_name="Local Development",
            must_change_password=False,
            is_bootstrap=True,
        )

    return get_authenticated_user(credentials)


def get_authenticated_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)]
) -> CurrentUser:
    """Authenticate a bearer token without honoring the local auth bypass."""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthError("Bearer token is required")

    token = credentials.credentials.strip()
    if not token:
        raise AuthError("Bearer token is required")
    return _user_from_token(token)


async def require_exact_admin(
    request: Request,
    user: Annotated[CurrentUser, Depends(get_authenticated_user)],
):
    if user.role != "admin":
        raise AuthError("Insufficient role permissions", status.HTTP_403_FORBIDDEN)
    if user.must_change_password:
        raise AuthError("Password change is required before using this endpoint", status.HTTP_403_FORBIDDEN)
    from request_limits import user_limits
    async with user_limits(request, user.id):
        yield user


def require_roles(*roles: str):
    minimum = min(ROLE_ORDER[role] for role in roles)

    async def dependency(request: Request, user: Annotated[CurrentUser, Depends(get_current_user)]):
        if ROLE_ORDER.get(user.role, 0) < minimum:
            raise AuthError("Insufficient role permissions", status.HTTP_403_FORBIDDEN)
        if user.must_change_password:
            raise AuthError("Password change is required before using this endpoint", status.HTTP_403_FORBIDDEN)
        from request_limits import user_limits
        async with user_limits(request, user.id):
            yield user

    return dependency


def require_roles_allowing_password_change(*roles: str):
    minimum = min(ROLE_ORDER[role] for role in roles)

    async def dependency(request: Request, user: Annotated[CurrentUser, Depends(get_current_user)]):
        if ROLE_ORDER.get(user.role, 0) < minimum:
            raise AuthError("Insufficient role permissions", status.HTTP_403_FORBIDDEN)
        from request_limits import user_limits
        async with user_limits(request, user.id):
            yield user

    return dependency


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def validate_password(password: str) -> None:
    if len(password) > 256:
        raise RagError("Password must not exceed 256 characters")
    if len(password) < 10:
        raise RagError("Password must be at least 10 characters")
    if not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password):
        raise RagError("Password must include at least one letter and one number")


def issue_refresh_token(user: CurrentUser, request: Request | None = None, metadata: dict | None = None) -> tuple[str, str, str]:
    token = generate_refresh_token()
    expires_at = datetime.now(UTC) + timedelta(days=_refresh_expires_days())
    row = {
        "user_id": user.id,
        "token_hash": hash_refresh_token(token),
        "expires_at": expires_at.isoformat(),
        "ip_address": request.client.host if request and request.client else None,
        "user_agent": request.headers.get("user-agent") if request else None,
        "metadata": metadata or {},
    }
    try:
        result = AuthRepository().issue_refresh_token(row, user.token_version)
    except Exception as exc:
        raise RagError("Could not issue refresh token.") from exc
    if not result:
        raise AuthError("Credentials changed. Please log in again.")
    return token, expires_at.isoformat(), result["id"]


def auth_token_bundle(user: CurrentUser, request: Request | None = None, metadata: dict | None = None) -> dict:
    access_token, access_expires_at = create_access_token(user)
    refresh_token, refresh_expires_at, _refresh_id = issue_refresh_token(user, request, metadata)
    return token_bundle(user, access_token, access_expires_at, refresh_token, refresh_expires_at)


def token_bundle(user: CurrentUser, access_token: str, access_expires_at: str, refresh_token: str, refresh_expires_at: str) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_at": access_expires_at,
        "refresh_expires_at": refresh_expires_at,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "must_change_password": user.must_change_password,
        },
    }


def create_app_user(
    email: str,
    password: str,
    role: str,
    full_name: str | None = None,
    metadata: dict | None = None,
    must_change_password: bool = True,
) -> dict:
    if role not in ROLE_ORDER:
        raise RagError(f"role must be one of: {', '.join(ROLE_ORDER)}")
    validate_password(password)
    row = {
        "email": email.strip().lower(),
        "full_name": full_name,
        "role": role,
        "password_hash": hash_password(password),
        "must_change_password": must_change_password,
        "metadata": metadata or {},
    }
    try:
        user = AuthRepository().create_user(row)
    except Exception as exc:
        raise RagError("Could not create user.") from exc
    user.pop("token_hash", None)
    user.pop("password_hash", None)
    return public_user(user)


def login_with_password(email: str, password: str, request: Request | None = None) -> dict:
    try:
        row = AuthRepository().get_user_for_auth(email)
    except Exception as exc:
        raise RagError("Could not load user.") from exc

    if not row:
        raise AuthError("Invalid email or password")

    if not row.get("password_hash") or not verify_password(password, row["password_hash"]):
        raise AuthError("Invalid email or password")

    user = _user_from_row(row)
    now = datetime.now(UTC).isoformat()
    try:
        AuthRepository().update_user(user.id, {"last_login_at": now, "updated_at": now})
    except Exception:
        pass
    return auth_token_bundle(user, request, {"action": "login"})


def refresh_access_token(refresh_token: str, request: Request | None = None) -> dict:
    _jwt_secret()  # Reject signing misconfiguration before consuming the old token.
    replacement = generate_refresh_token()
    expires_at = (datetime.now(UTC) + timedelta(days=_refresh_expires_days())).isoformat()
    try:
        result = AuthRepository().rotate_refresh_token(
            hash_refresh_token(refresh_token.strip()), hash_refresh_token(replacement), expires_at,
            request.client.host if request and request.client else None,
            request.headers.get("user-agent") if request else None,
        )
    except Exception as exc:
        raise AppError("Could not refresh session. Please try again.", code=ErrorCode.STORAGE_ERROR, status_code=503) from exc
    if not result:
        raise AuthError("Invalid, expired, or already used refresh token")
    user = _user_from_row(result["user"])
    access_token, access_expires_at = create_access_token(user)
    return token_bundle(user, access_token, access_expires_at, replacement, result["expires_at"])


def change_password(user: CurrentUser, current_password: str, new_password: str) -> dict:
    validate_password(new_password)
    repository = AuthRepository()
    try:
        row = repository.get_user_by_id(user.id, "id,password_hash")
    except Exception as exc:
        raise RagError("Could not load user.") from exc
    if not row or not row.get("password_hash"):
        raise AuthError("Password cannot be changed for this user")
    if not verify_password(current_password, row["password_hash"]):
        raise AuthError("Current password is incorrect")
    try:
        updated = repository.change_password(user.id, row["password_hash"], hash_password(new_password), False)
    except Exception as exc:
        raise AppError("Could not change password.", code=ErrorCode.STORAGE_ERROR, status_code=503) from exc
    if not updated:
        raise AuthError("Credentials changed. Please log in again.")
    return {"changed": True, "user": _user_from_row(updated)}


def update_own_profile(user: CurrentUser, full_name: str | None = None) -> dict:
    updates = {"updated_at": datetime.now(UTC).isoformat()}
    if full_name is not None:
        updates["full_name"] = full_name
    try:
        row = AuthRepository().update_user(user.id, updates)
    except Exception as exc:
        raise RagError("Could not update profile.") from exc
    return public_user(row)


def update_app_user(
    user_id: str,
    email: str | None = None,
    full_name: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    metadata: dict | None = None,
) -> dict:
    updates = {"updated_at": datetime.now(UTC).isoformat()}
    if email is not None:
        updates["email"] = email.strip().lower()
    if full_name is not None:
        updates["full_name"] = full_name
    if role is not None:
        if role not in ROLE_ORDER:
            raise RagError(f"role must be one of: {', '.join(ROLE_ORDER)}")
        updates["role"] = role
    if is_active is not None:
        updates["is_active"] = is_active
    if metadata is not None:
        updates["metadata"] = metadata
    try:
        row = AuthRepository().update_user(user_id, updates)
    except Exception as exc:
        raise RagError("Could not update user.") from exc
    if not row:
        raise RagError(f"User not found: {user_id}")
    return public_user(row)


def reset_app_user_password(user_id: str, new_password: str, must_change_password: bool = True) -> dict:
    validate_password(new_password)
    try:
        row = AuthRepository().change_password(user_id, None, hash_password(new_password), must_change_password)
    except Exception as exc:
        raise AppError("Could not reset password.", code=ErrorCode.STORAGE_ERROR, status_code=503) from exc
    if not row:
        raise RagError(f"User not found: {user_id}")
    return public_user(row)


def audit_event(
    request: Request,
    actor: CurrentUser,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    row = {
        "actor_user_id": actor.id,
        "actor_email": actor.email,
        "actor_role": actor.role,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "metadata": metadata or {},
    }
    try:
        AuthRepository().insert_audit_event(row)
    except Exception:
        # Audit failures must not break the user flow, but production logging should alert on them.
        logger.exception(
            "audit_event_insert_failed",
            extra={
                "actor_user_id": actor.id,
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
            },
        )
        return


def list_audit_events(limit: int = 100) -> list[dict]:
    try:
        return AuthRepository().list_audit_events(limit)
    except Exception as exc:
        raise RagError("Could not list audit events.") from exc


def list_app_users(limit: int = 100) -> list[dict]:
    try:
        rows = AuthRepository().list_users(limit)
    except Exception as exc:
        raise RagError("Could not list users.") from exc
    return [public_user(row) for row in rows]
