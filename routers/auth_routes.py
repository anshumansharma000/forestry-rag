from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth import (
    CurrentUser,
    audit_event,
    auth_token_bundle,
    change_password,
    login_with_password,
    refresh_access_token,
    require_roles,
    require_roles_allowing_password_change,
    update_own_profile,
)
from schemas import AuthTokenResponse, ChangePasswordRequest, LoginRequest, RefreshRequest, UpdateOwnProfileRequest, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=UserResponse)
def auth_me(user: CurrentUser = Depends(require_roles_allowing_password_change("viewer"))):
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "must_change_password": user.must_change_password,
        "is_bootstrap": user.is_bootstrap,
    }


@router.post("/login", response_model=AuthTokenResponse)
def auth_login(request: Request, request_body: LoginRequest):
    result = login_with_password(request_body.email, request_body.password, request)
    audit_user = CurrentUser(
        id=result["user"]["id"],
        email=result["user"]["email"],
        role=result["user"]["role"],
        full_name=result["user"].get("full_name"),
        must_change_password=result["user"].get("must_change_password", False),
    )
    audit_event(request, audit_user, "auth.login", "app_user", audit_user.id)
    return result


@router.post("/refresh", response_model=AuthTokenResponse)
def auth_refresh(request: Request, request_body: RefreshRequest):
    return refresh_access_token(request_body.refresh_token, request)


@router.post("/change-password", response_model=AuthTokenResponse)
def auth_change_password(
    request: Request,
    request_body: ChangePasswordRequest,
    user: CurrentUser = Depends(require_roles_allowing_password_change("viewer")),
):
    if user.is_bootstrap:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bootstrap user cannot change password")
    change_password(user, request_body.current_password, request_body.new_password)
    audit_event(request, user, "auth.password_change", "app_user", user.id)
    updated_user = CurrentUser(
        id=user.id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
        must_change_password=False,
    )
    result = auth_token_bundle(updated_user, request, {"action": "password_change"})
    result["changed"] = True
    return result


@router.patch("/me", response_model=UserResponse)
def auth_update_me(
    request: Request,
    request_body: UpdateOwnProfileRequest,
    user: CurrentUser = Depends(require_roles("viewer")),
):
    result = update_own_profile(user, request_body.full_name)
    audit_event(request, user, "auth.profile_update", "app_user", user.id)
    return result
