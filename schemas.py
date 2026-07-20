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
    section_heading: str | None = None
    score: float
    evidence_role: str = "matched"
    text: str


class AskResponse(BaseModel):
    answer: str
    sources: list[SourceResponse]
    confidence: float | None = None
    abstained: bool = False


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


class IngestRequest(BaseModel):
    source: NonEmptyStr | None = Field(default=None, max_length=255)


class UploadDocumentResponse(BaseModel):
    status: str
    filename: str
    path: str


class UploadDocumentsResponse(BaseModel):
    status: str
    files: list[UploadDocumentResponse]


class DocumentLibraryItemResponse(BaseModel):
    id: UUID | str
    filename: str
    title: str
    kind: str
    page_count: int | None = None
    document_type: str
    authority: str | None = None
    years: list[str] = Field(default_factory=list)
    chunk_count: int
    status: str
    ingested_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class DocumentLibraryPaginationResponse(BaseModel):
    offset: int
    limit: int
    total: int
    has_more: bool


class DocumentLibraryResponse(BaseModel):
    items: list[DocumentLibraryItemResponse]
    pagination: DocumentLibraryPaginationResponse


class DirectUploadFileRequest(BaseModel):
    filename: NonEmptyStr
    size_bytes: int = Field(gt=0)
    content_type: str | None = Field(default=None, max_length=255)


class CreatePresignedUploadsRequest(BaseModel):
    files: list[DirectUploadFileRequest] = Field(min_length=1, max_length=50)


class PresignedUploadResponse(BaseModel):
    upload_id: UUID | str
    filename: str
    upload_url: str
    method: str
    headers: dict[str, str]
    expires_in_seconds: int
    max_bytes: int


class PresignedUploadsResponse(BaseModel):
    status: str
    uploads: list[PresignedUploadResponse]


class CompleteDirectUploadFileRequest(BaseModel):
    upload_id: UUID
    filename: NonEmptyStr


class CompleteDirectUploadsRequest(BaseModel):
    files: list[CompleteDirectUploadFileRequest] = Field(min_length=1, max_length=50)


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
