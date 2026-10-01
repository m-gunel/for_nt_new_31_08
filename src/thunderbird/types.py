from datetime import datetime

from pydantic import BaseModel, Field

# filepath: src/thunderbird/schemas.py


class AttachmentModel(BaseModel):
    name: str | None = None
    doc_type: str | None = None
    size: int | None = None
    field_1: str | None = None
    field_2: str | None = None
    field_3: bool | None = None


class MessageModel(BaseModel):
    id: int
    folderID: int | None = None
    headerMessageID: str | None = None
    conversationID: str | None = None
    date: datetime | None = None
    c0body: str | None = None
    c1subject: str | None = None
    from_: int | None = None
    from_name: str | None = None
    to: list[int] = Field(default_factory=list)
    cc: list[int] = Field(default_factory=list)
    bcc: list[int] = Field(default_factory=list)
    attachments: list[AttachmentModel] = Field(default_factory=list)
    from_me: bool = False
    to_me: bool = False
    score: float | None = None
    is_best: bool = False
