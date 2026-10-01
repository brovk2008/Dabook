"""
Book Graph Intermediate Representation (IR) Models (§9).

Provides typed, serialized data structures for pages, text blocks,
bounding boxes, and the complete Book Graph with provenance tracking.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BBox(BaseModel):
    """Bounding box in PDF points (x0, y0, x1, y1) where (0,0) is top-left."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self) -> float:
        return max(0.0, self.y1 - self.y0)

    def to_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2), round(self.x1, 2), round(self.y1, 2)]


class TextLine(BaseModel):
    """A single line of text with its bounding box."""

    text: str
    bbox: list[float]
    font_size: float = 10.0
    is_bold: bool = False


class RawBlock(BaseModel):
    """A semantic text block extracted from a page."""

    block_id: str
    page_idx: int
    bbox: list[float]
    raw_text: str
    normalized_text: str = ""
    clean_text: str = ""
    block_type: str = (
        "paragraph"  # heading, paragraph, code, table, equation, furniture, caption, list_item
    )
    confidence: float = 1.0
    font_name: str | None = None
    font_size: float = 10.0
    is_bold: bool = False
    is_italic: bool = False
    lines: list[TextLine] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PageData(BaseModel):
    """All extracted content and geometry for a single page."""

    page_idx: int
    width: float
    height: float
    blocks: list[RawBlock] = Field(default_factory=list)
    is_scanned: bool = False
    char_count: int = 0
    image_count: int = 0


class TocItem(BaseModel):
    """Table of contents entry."""

    title: str
    level: int
    page_idx: int
    block_id: str | None = None


class BookNode(BaseModel):
    """A node in the Book Graph with parent/child relationships."""

    node_id: str
    node_type: str  # book, chapter, section, paragraph, code, table, list
    title: str | None = None
    text: str = ""
    page_idx: int = 0
    bbox: list[float] = Field(default_factory=list)
    parent_id: str | None = None
    children_ids: list[str] = Field(default_factory=list)
    breadcrumbs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BookGraph(BaseModel):
    """Complete traceable Book Graph."""

    book_id: int
    sha256: str
    title: str
    total_pages: int
    nodes: list[BookNode] = Field(default_factory=list)
    toc: list[TocItem] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
