from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class StatusResponse(BaseModel):
    status: str = "ok"


class DataResponse(BaseModel, Generic[T]):
    data: T
    metadata: dict[str, Any] = Field(default_factory=dict)


class ListResponse(BaseModel, Generic[T]):
    items: list[T]
    metadata: dict[str, Any] = Field(default_factory=dict)
