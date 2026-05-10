import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth import (
    CurrentUser,
    audit_event,
    create_app_user,
    list_app_users,
    list_audit_events,
    require_roles,
    reset_app_user_password,
    update_app_user,
)
from schemas import CreateUserRequest, ResetPasswordRequest, UpdateUserRequest, UserResponse

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)


@router.post("/users", response_model=UserResponse)
def create_user(
    request: Request,
    request_body: CreateUserRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    logger.info("admin_user_create_requested", extra={"email": str(request_body.email), "actor_user_id": user.id})
    created = create_app_user(
        str(request_body.email),
        request_body.password,
        request_body.role,
        full_name=request_body.full_name,
        metadata=request_body.metadata,
        must_change_password=request_body.must_change_password,
    )
    audit_event(request, user, "users.create", "app_user", created["id"], {"email": created["email"], "role": created["role"]})
    return created


@router.get("/users")
def admin_list_users(limit: int = 100, _user: CurrentUser = Depends(require_roles("admin"))):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be between 1 and 500")
    return {"users": list_app_users(limit)}


@router.patch("/users/{user_id}", response_model=UserResponse)
def admin_update_user(
    request: Request,
    user_id: str,
    request_body: UpdateUserRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    updated = update_app_user(
        user_id,
        email=str(request_body.email) if request_body.email is not None else None,
        full_name=request_body.full_name,
        role=request_body.role,
        is_active=request_body.is_active,
        metadata=request_body.metadata,
    )
    audit_event(request, user, "users.update", "app_user", user_id)
    return updated


@router.post("/users/{user_id}/reset-password", response_model=UserResponse)
def admin_reset_password(
    request: Request,
    user_id: str,
    request_body: ResetPasswordRequest,
    user: CurrentUser = Depends(require_roles("admin")),
):
    updated = reset_app_user_password(user_id, request_body.new_password, request_body.must_change_password)
    audit_event(request, user, "users.password_reset", "app_user", user_id)
    return updated


@router.get("/audit-events")
def audit_events(limit: int = 100, _user: CurrentUser = Depends(require_roles("admin"))):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be between 1 and 500")
    return {"events": list_audit_events(limit)}
