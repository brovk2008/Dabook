"""
PDF extraction backend using pypdfium2 (§8.2).

Fast, permissive C-level PDF parsing. Extracts character-level bounding boxes,
word runs, lines, page dimensions, and embedded bookmarks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from dabook.ir.models import PageData, RawBlock, TextLine, TocItem

logger = logging.getLogger("dabook.backends.pdfium")


class PdfiumExtractor:
    """Extracts text, bounding boxes, and structure from a PDF file."""

    def __init__(self, pdf_path: Path) -> None:
        self.pdf_path = pdf_path
        self._doc: pdfium.PdfDocument | None = None

    def __enter__(self) -> PdfiumExtractor:
        self._doc = pdfium.PdfDocument(self.pdf_path)
        return self

    def __exit__(self, *args: Any) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    @property
    def doc(self) -> pdfium.PdfDocument:
        if self._doc is None:
            self._doc = pdfium.PdfDocument(self.pdf_path)
        return self._doc

    def page_count(self) -> int:
        return len(self.doc)

    def extract_toc(self) -> list[TocItem]:
        """Extract embedded PDF bookmarks / table of contents."""
        items: list[TocItem] = []
        try:
            for bm in self.doc.get_toc():
                title = bm.get_title().strip() if hasattr(bm, "get_title") else ""
                if not title:
                    continue
                dest = bm.get_dest()
                page_idx = dest.get_index() if dest else 0
                items.append(
                    TocItem(
                        title=title,
                        level=bm.level,
                        page_idx=page_idx or 0,
                    )
                )
        except Exception as e:
            logger.warning("Failed to extract TOC: %s", e)
        return items

    def extract_page(self, page_idx: int) -> PageData:
        """Extract all blocks, lines, and bounding boxes for a single page."""
        page = self.doc[page_idx]
        pw, ph = page.get_size()
        tp = page.get_textpage()

        char_count = tp.count_chars()
        if char_count == 0:
            return PageData(
                page_idx=page_idx,
                width=pw,
                height=ph,
                is_scanned=True,
                char_count=0,
            )

        n_rects = tp.count_rects()
        raw_rects: list[tuple[float, float, float, float, str, float]] = []

        for i in range(n_rects):
            rx0, ry0_pdf, rx1, ry1_pdf = tp.get_rect(i)
            # In PDF coordinates: bottom is 0. Convert to top-left origin:
            x0 = max(0.0, rx0)
            y0 = max(0.0, ph - ry1_pdf)
            x1 = min(pw, rx1)
            y1 = min(ph, ry0_pdf)
            h = max(1.0, y1 - y0)
            text = tp.get_text_bounded(rx0, ry0_pdf, rx1, ry1_pdf).strip()
            if text:
                raw_rects.append((x0, y0, x1, y1, text, h))

        # Group rects into lines by vertical alignment (y0 within threshold)
        lines: list[TextLine] = []
        raw_text_full = tp.get_text_range()
        split_lines = [ln.strip() for ln in raw_text_full.splitlines() if ln.strip()]

        # Estimate average font size
        font_sizes = [r[5] for r in raw_rects if r[5] > 0]
        avg_font_size = sum(font_sizes) / len(font_sizes) if font_sizes else 10.0

        blocks: list[RawBlock] = []
        block_idx = 0

        # Construct lines with estimated bounding boxes
        for line_idx, line_text in enumerate(split_lines):
            # Estimate bbox from matching rects or linear distribution
            matching = [r for r in raw_rects if r[4] in line_text or line_text in r[4]]
            if matching:
                min_x = min(r[0] for r in matching)
                min_y = min(r[1] for r in matching)
                max_x = max(r[2] for r in matching)
                max_y = max(r[3] for r in matching)
                line_fs = max(r[5] for r in matching)
            else:
                y_pos = (line_idx / max(1, len(split_lines))) * ph
                min_x, min_y, max_x, max_y = 50.0, y_pos, pw - 50.0, y_pos + avg_font_size
                line_fs = avg_font_size

            lines.append(
                TextLine(
                    text=line_text,
                    bbox=[round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)],
                    font_size=round(line_fs, 1),
                    is_bold=line_fs > avg_font_size * 1.15,
                )
            )

        # Group consecutive lines into semantic blocks
        current_lines: list[TextLine] = []
        for line in lines:
            if not current_lines:
                current_lines.append(line)
                continue

            prev = current_lines[-1]
            vert_gap = line.bbox[1] - prev.bbox[3]

            # Break block on large vertical gap, or significant font size difference, or bullet/heading
            is_heading = line.font_size > avg_font_size * 1.25
            is_bullet = line.text.startswith(("■", "•", "-", "*", "1.", "2.", "3."))
            is_code_start = line.text.startswith(
                ("push ", "mov ", "pop ", "call ", "int ", "sub ", "add ", "xor ", "0x")
            )

            if vert_gap > prev.font_size * 1.6 or is_heading or is_bullet or is_code_start:
                # Flush block
                block = self._create_block(page_idx, block_idx, current_lines, avg_font_size)
                blocks.append(block)
                block_idx += 1
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            block = self._create_block(page_idx, block_idx, current_lines, avg_font_size)
            blocks.append(block)

        return PageData(
            page_idx=page_idx,
            width=pw,
            height=ph,
            blocks=blocks,
            is_scanned=False,
            char_count=char_count,
        )

    def _create_block(
        self,
        page_idx: int,
        block_idx: int,
        lines: list[TextLine],
        avg_font_size: float,
    ) -> RawBlock:
        x0 = min(ln.bbox[0] for ln in lines)
        y0 = min(ln.bbox[1] for ln in lines)
        x1 = max(ln.bbox[2] for ln in lines)
        y1 = max(ln.bbox[3] for ln in lines)
        full_text = " ".join(ln.text for ln in lines)
        max_fs = max(ln.font_size for ln in lines)
        is_bold = any(ln.is_bold for ln in lines)

        # Detect block type
        block_type = "paragraph"
        if max_fs > avg_font_size * 1.25:
            block_type = "heading"
        elif any(
            ln.text.startswith(
                ("push ", "mov ", "pop ", "call ", "sub ", "add ", "xor ", "0x", "int ")
            )
            for ln in lines
        ):
            block_type = "code"
        elif lines[0].text.startswith(("■", "•", "-", "*")):
            block_type = "list_item"

        return RawBlock(
            block_id=f"p{page_idx}_b{block_idx}",
            page_idx=page_idx,
            bbox=[round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            raw_text=full_text,
            normalized_text=full_text,
            clean_text=full_text,
            block_type=block_type,
            font_size=round(max_fs, 1),
            is_bold=is_bold,
            lines=lines,
        )
