"""
PDF extraction backend using pypdfium2 (§8.2).

Fast, permissive C-level PDF parsing. Extracts character-level bounding boxes,
word runs, lines, page dimensions, embedded bookmarks, and exact font metrics
from PDF graphics-state objects.
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
        """Extract all blocks, lines, and bounding boxes for a single page with graphics state."""
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

        # 1. Extract visual spans with graphics-state metadata
        spans: list[dict[str, Any]] = []
        try:
            for obj in page.get_objects():
                if not isinstance(obj, pdfium.PdfTextObj):
                    continue
                obj.textpage = tp
                raw_text = obj.extract()
                if not raw_text:
                    continue

                font = obj.get_font()
                fname = (font.get_family_name() or "") if hasattr(font, "get_family_name") else ""
                weight = font.get_weight() if hasattr(font, "get_weight") else 400
                fs = obj.get_font_size() if hasattr(obj, "get_font_size") else 10.0

                bounds = obj.get_bounds()
                x0 = max(0.0, bounds[0])
                top_y = max(0.0, ph - bounds[3])
                x1 = min(pw, bounds[2])
                bot_y = max(top_y + 1.0, ph - bounds[1])

                fname_lower = fname.lower()
                is_mono = any(
                    m in fname_lower
                    for m in [
                        "courier",
                        "mono",
                        "code",
                        "consolas",
                        "inconsolata",
                        "typewriter",
                        "menlo",
                    ]
                )

                spans.append(
                    {
                        "x0": x0,
                        "y0": top_y,
                        "x1": x1,
                        "y1": bot_y,
                        "text": raw_text,
                        "font": fname,
                        "weight": weight,
                        "font_size": fs,
                        "is_mono": is_mono,
                    }
                )
        except Exception as exc:
            logger.debug("Object extraction error on page %d: %s", page_idx, exc)

        # Fallback to textpage rects if no spans were extracted
        if not spans:
            return self._extract_page_fallback(page_idx, page, pw, ph, tp, char_count)

        # 2. Sort spans top-to-bottom, left-to-right
        spans.sort(key=lambda s: (round(s["y0"] / 3.5), s["x0"]))

        # 3. Horizontal baseline clustering into visual lines
        lines_spans: list[list[dict[str, Any]]] = []
        curr_line: list[dict[str, Any]] = []
        for s in spans:
            if not curr_line:
                curr_line.append(s)
                continue
            if abs(s["y0"] - curr_line[0]["y0"]) <= 3.5:
                curr_line.append(s)
            else:
                lines_spans.append(curr_line)
                curr_line = [s]
        if curr_line:
            lines_spans.append(curr_line)

        assembled_lines: list[TextLine] = []
        for l_spans in lines_spans:
            l_spans.sort(key=lambda s: s["x0"])
            line_text = ""
            for i, s in enumerate(l_spans):
                t = s["text"]
                # Ligature healing on adjacent spans (e.g. 'defi' + 'fi ned')
                if line_text.endswith(("fi", "fl", "ff")) and t.startswith(("fi ", "fl ", "ff ")):
                    t = t[3:]
                elif (
                    line_text
                    and not line_text.endswith((" ", "-", "[", "("))
                    and not t.startswith((" ", "]", ")", ",", ".", ";", ":"))
                    and s["x0"] - l_spans[i - 1]["x1"] > 2.0
                ):
                    line_text += " "
                line_text += t

            clean_line_text = line_text.strip()
            if not clean_line_text:
                continue

            mono_chars = sum(len(s["text"]) for s in l_spans if s["is_mono"])
            total_chars = max(1, sum(len(s["text"]) for s in l_spans))
            is_line_mono = (mono_chars / total_chars >= 0.5) or (
                l_spans[0]["is_mono"]
                and clean_line_text.startswith(
                    (
                        "01:",
                        "02:",
                        ";",
                        "//",
                        "mov",
                        "push",
                        "pop",
                        "add",
                        "sub",
                        "xor",
                        "call",
                        "ret",
                    )
                )
            )

            avg_weight = sum(s["weight"] for s in l_spans) / len(l_spans)
            max_fs = max(s["font_size"] for s in l_spans)
            assembled_lines.append(
                TextLine(
                    text=clean_line_text,
                    bbox=[
                        round(min(s["x0"] for s in l_spans), 2),
                        round(min(s["y0"] for s in l_spans), 2),
                        round(max(s["x1"] for s in l_spans), 2),
                        round(max(s["y1"] for s in l_spans), 2),
                    ],
                    font_size=round(max_fs, 1),
                    is_bold=avg_weight >= 600,
                    is_mono=is_line_mono,
                )
            )

        # 4. Group lines into cohesive semantic blocks
        blocks: list[RawBlock] = []
        block_idx = 0
        current_lines: list[TextLine] = []

        for line in assembled_lines:
            if not current_lines:
                current_lines.append(line)
                continue

            prev = current_lines[-1]
            vert_gap = line.bbox[1] - prev.bbox[3]

            is_mono_transition = line.is_mono != prev.is_mono
            is_heading = (
                line.is_bold and len(line.text) < 120 and not line.text.endswith((".", ";", ":"))
            )
            is_gap = vert_gap > max(prev.font_size, 10.0) * 1.5
            is_bullet = line.text.startswith(("■", "•", "-", "*", "1.", "2.", "3."))

            if is_mono_transition or is_heading or is_gap or is_bullet:
                blocks.append(self._create_block(page_idx, block_idx, current_lines))
                block_idx += 1
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            blocks.append(self._create_block(page_idx, block_idx, current_lines))

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
    ) -> RawBlock:
        x0 = min(ln.bbox[0] for ln in lines)
        y0 = min(ln.bbox[1] for ln in lines)
        x1 = max(ln.bbox[2] for ln in lines)
        y1 = max(ln.bbox[3] for ln in lines)

        mono_chars = sum(len(ln.text) for ln in lines if ln.is_mono)
        total_chars = max(1, sum(len(ln.text) for ln in lines))
        is_all_mono = any(ln.is_mono for ln in lines) and (mono_chars / total_chars >= 0.5)

        is_heading = (
            lines[0].is_bold
            and len(lines) <= 2
            and sum(len(ln.text) for ln in lines) < 120
            and not lines[-1].text.endswith((".", ";"))
        )
        is_bullet = lines[0].text.startswith(("■", "•", "-", "*"))

        if is_all_mono:
            block_type = "code"
            base_x = min(ln.bbox[0] for ln in lines)
            indented = []
            for ln in lines:
                indent = max(0, round((ln.bbox[0] - base_x) / 6.0))
                indented.append((" " * indent) + ln.text)
            full_text = "\n".join(indented)
        elif is_heading:
            block_type = "heading"
            full_text = " ".join(ln.text for ln in lines)
        elif is_bullet:
            block_type = "list_item"
            full_text = " ".join(ln.text for ln in lines)
        else:
            block_type = "paragraph"
            full_text = " ".join(ln.text for ln in lines)

        return RawBlock(
            block_id=f"p{page_idx}_b{block_idx}",
            page_idx=page_idx,
            bbox=[round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            raw_text=full_text,
            normalized_text=full_text,
            clean_text=full_text,
            block_type=block_type,
            font_size=round(max(ln.font_size for ln in lines), 1),
            is_bold=lines[0].is_bold,
            is_mono=is_all_mono,
            lines=lines,
        )

    def _extract_page_fallback(
        self,
        page_idx: int,
        page: Any,
        pw: float,
        ph: float,
        tp: Any,
        char_count: int,
    ) -> PageData:
        """Fallback extractor using raw textpage if object graphics state is unavailable."""
        raw_text_full = tp.get_text_range()
        split_lines = [ln.strip() for ln in raw_text_full.splitlines() if ln.strip()]
        lines: list[TextLine] = []
        for line_idx, line_text in enumerate(split_lines):
            y_pos = (line_idx / max(1, len(split_lines))) * ph
            lines.append(
                TextLine(
                    text=line_text,
                    bbox=[50.0, round(y_pos, 2), pw - 50.0, round(y_pos + 12.0, 2)],
                    font_size=10.0,
                    is_bold=False,
                    is_mono=False,
                )
            )
        blocks = [
            RawBlock(
                block_id=f"p{page_idx}_b0",
                page_idx=page_idx,
                bbox=[50.0, 50.0, pw - 50.0, ph - 50.0],
                raw_text="\n".join(split_lines),
                normalized_text="\n".join(split_lines),
                clean_text="\n".join(split_lines),
                block_type="paragraph",
                lines=lines,
            )
        ]
        return PageData(
            page_idx=page_idx,
            width=pw,
            height=ph,
            blocks=blocks,
            is_scanned=False,
            char_count=char_count,
        )
