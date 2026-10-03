from typing import Any

from pydantic import BaseModel, Field


class ParseWarningOut(BaseModel):
    code: str
    message: str
    location: str | None = None


class ParseResponse(BaseModel):
    filename: str
    document_type: str
    strategy_requested: str
    strategy_used: str
    record_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    records: list[dict[str, Any]]
    warnings: list[ParseWarningOut]
