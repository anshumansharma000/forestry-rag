from typing import Annotated, Any, Literal
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


class RagLabChunkingConfig(BaseModel):
    strategy: Literal["structure_aware_v1"] = "structure_aware_v1"
    profile: Literal["auto", "section", "faq", "procedure"] = "auto"
    max_tokens: int = Field(default=600, ge=100, le=2000)
    overlap_tokens: int = Field(default=100, ge=0, le=500)

    def model_post_init(self, _context: Any) -> None:
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be smaller than max_tokens")


class RagLabRetrievalConfig(BaseModel):
    top_k: int = Field(default=5, ge=1, le=20)
    candidate_count: int = Field(default=40, ge=1, le=200)
    max_per_source: int = Field(default=0, ge=0, le=20)
    duplicate_threshold: float = Field(default=0.82, ge=0, le=1)
    min_context_score: float = Field(default=0, ge=0, le=1)
    expand_neighbors: bool = True


class RagLabConfig(BaseModel):
    chunking: RagLabChunkingConfig = Field(default_factory=RagLabChunkingConfig)
    retrieval: RagLabRetrievalConfig = Field(default_factory=RagLabRetrievalConfig)


class CreateRagLabExperimentRequest(BaseModel):
    name: ShortText
    description: str | None = Field(default=None, max_length=2000)
    config: RagLabConfig = Field(default_factory=RagLabConfig)


class UpdateRagLabExperimentRequest(BaseModel):
    name: ShortText | None = None
    description: str | None = Field(default=None, max_length=2000)
    config: RagLabConfig | None = None


class CreateRagLabRevisionRequest(BaseModel):
    config: RagLabConfig | None = None


class RagLabQueryRequest(BaseModel):
    question: NonEmptyStr
    retrieval: RagLabRetrievalConfig | None = None


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
    document_id: str
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
    ingest_error: str | None = None
    retryable: bool = False
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
    sources: list[SourceResponse | dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None


class ChatAskResponse(BaseModel):
    session_id: UUID | str
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    search_query: str
    answer: str
    sources: list[SourceResponse]
    confidence: float | None = None
    abstained: bool = False


class ChatMessagesResponse(BaseModel):
    session_id: UUID | str
    messages: list[ChatMessageResponse]
