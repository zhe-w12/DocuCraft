from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)


class AssistantChatRequest(BaseModel):
    mode: Literal["auto", "write", "revise", "polish", "shorten", "expand", "rewrite", "summarize", "chat"] = "auto"
    messages: list[AssistantMessage] = Field(min_length=1, max_length=80)
    stream: bool = False
    web_search: bool = False

    @model_validator(mode="after")
    def validate_conversation(self):
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("对话中至少需要一条用户消息")
        if sum(len(message.content) for message in self.messages) > 120_000:
            raise ValueError("对话内容过长，请新建对话后继续")
        return self


class AssistantChatResponse(BaseModel):
    content: str
    provider: str
    model: str


class AssistantExportRequest(BaseModel):
    title: str = Field(default="未命名文档", max_length=200)
    content: str = Field(min_length=1, max_length=100_000)


class DocumentResponse(BaseModel):
    id: str
    title: str | None = None
    status: str = "completed"
    has_tables: bool = False
    docx_url: str | None = None
    docx_preserves_source_layout: bool = False
    structured_docx_url: str | None = None
    pdf_url: str | None = None
    pdf_preserves_source_layout: bool = False
    structured_pdf_url: str | None = None
    warnings: list[str] = Field(default_factory=list)
    paragraphs: list[str] = Field(default_factory=list)
    paragraph_styles: list[str] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list)
    # Source-image geometry is returned so the mobile offline editor can keep
    # table cells editable even when it cannot load the server HTML editor.
    table_layouts: list[dict] = Field(default_factory=list)
    table_cells: list[list[dict]] = Field(default_factory=list)
    image_width: int = 0
    image_height: int = 0
    # Keep the source-coordinate OCR blocks available to the mobile client.
    # Some table scans contain a visible heading that the OCR provider does
    # not put into the paragraph list, but it is still present in this layer.
    positioned_words: list[dict[str, str | float | bool]] = Field(default_factory=list)
    editor_html: str | None = None
    source_image_url: str | None = None


class PositionedWordRequest(BaseModel):
    source_index: int | None = Field(default=None, ge=0, le=1_000_000)
    text: str = Field(default="", max_length=10_000)
    left: float = Field(default=0.0, ge=0.0, le=100_000_000, allow_inf_nan=False)
    top: float = Field(default=0.0, ge=0.0, le=100_000_000, allow_inf_nan=False)
    width: float = Field(default=0.0, ge=0.0, le=100_000_000, allow_inf_nan=False)
    height: float = Field(default=0.0, ge=0.0, le=100_000_000, allow_inf_nan=False)
    is_form_field: bool = False
    rendered_width: float | None = Field(
        default=None,
        ge=0.0,
        le=100_000_000,
        allow_inf_nan=False,
    )


class EditedDocumentRequest(BaseModel):
    document_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    paragraphs: list[str] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list)
    positioned_words: list[PositionedWordRequest] = Field(default_factory=list)
