from fastapi import APIRouter, Depends, HTTPException, status

from auth import CurrentUser, require_roles
from responses import StatusResponse
from settings import config_status, validate_runtime_config

router = APIRouter(tags=["system"])


@router.get("/health", response_model=StatusResponse)
def health():
    return StatusResponse()


@router.get("/config/status")
def runtime_config_status():
    return config_status()


@router.get("/config/validate")
def runtime_config_validate(_user: CurrentUser = Depends(require_roles("admin"))):
    result = validate_runtime_config()
    if not result["ok"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result)
    return result
