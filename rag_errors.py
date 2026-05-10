from errors import AppError, ErrorCode


class RagError(AppError):
    def __init__(self, message: str, *, code: ErrorCode | str = ErrorCode.RAG_ERROR, details: dict | None = None):
        super().__init__(message, code=code, details=details)
