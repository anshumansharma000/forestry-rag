from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
PasswordStr = Annotated[str, StringConstraints(min_length=10, max_length=256)]


class AskRequest(BaseModel):
    question: NonEmptyStr
    top_k: int | None = Field(default=None, ge=1, le=20)


class CreateChatSessionRequest(BaseModel):
    title: ShortText | None = None


class ChatAskRequest(BaseModel):
    message: NonEmptyStr
    top_k: int | None = Field(default=None, ge=1, le=20)


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: PasswordStr
    role: str = Field(pattern="^(viewer|officer|knowledge_manager|admin)$")
    full_name: ShortText | None = None
    metadata: dict[str, Any] | None = None
    must_change_password: bool = True


class LoginRequest(BaseModel):
    email: EmailStr
    password: NonEmptyStr


class RefreshRequest(BaseModel):
    refresh_token: NonEmptyStr


class ChangePasswordRequest(BaseModel):
    current_password: NonEmptyStr
    new_password: PasswordStr


class UpdateOwnProfileRequest(BaseModel):
    full_name: ShortText | None = None


class UpdateUserRequest(BaseModel):
    email: EmailStr | None = None
    full_name: ShortText | None = None
    role: str | None = Field(default=None, pattern="^(viewer|officer|knowledge_manager|admin)$")
    is_active: bool | None = None
    metadata: dict[str, Any] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: PasswordStr
    must_change_password: bool = True


class UserResponse(BaseModel):
    id: UUID | str
    email: str
    full_name: str | None = None
    role: str
    is_active: bool | None = None
    must_change_password: bool = False
    last_login_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class AuthTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    expires_at: str
    refresh_expires_at: str
    user: dict[str, Any]
    changed: bool | None = None


class SourceResponse(BaseModel):
    source: str
    display_source: str
    page_start: int | None = None
    page_end: int | None = None
    chunk_index: int
    score: float
    text: str


class AskResponse(BaseModel):
    answer: str
    sources: list[SourceResponse]


class IngestJobResponse(BaseModel):
    id: UUID | str
    kind: str
    status: str
    actor_user_id: UUID | str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class IngestJobEnvelope(BaseModel):
    job: IngestJobResponse


class UploadDocumentResponse(BaseModel):
    status: str
    filename: str
    path: str


class ChatSessionResponse(BaseModel):
    id: UUID | str
    user_id: UUID | str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ChatMessageResponse(BaseModel):
    id: UUID | str
    session_id: UUID | str
    role: str
    content: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
