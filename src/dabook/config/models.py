"""
Configuration models — pydantic v2 typed config for dabook.toml.

Precedence: CLI args > env vars > dabook.toml > profile defaults > hardcoded defaults.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class WorkersConfig(BaseModel):
    cpu: int = Field(default=4, ge=1, description="CPU worker count")
    gpu: int = Field(default=1, ge=0)
    io: int = Field(default=2, ge=1)
    llm: int = Field(default=0, ge=0)
    book_concurrency: int = Field(default=2, ge=1)
    autotune: bool = False


class LimitsConfig(BaseModel):
    ram_ceiling_pct: float = Field(default=85.0, ge=10, le=99)
    vram_ceiling_pct: float = Field(default=90.0, ge=10, le=99)
    min_free_gb: float = Field(default=5.0, ge=0.5)
    shard_pages: int = Field(default=20, ge=1, le=500)
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_s: float = Field(default=5.0, ge=1.0)
    lease_s: float = Field(default=90.0, ge=30.0)
    heartbeat_s: float = Field(default=15.0, ge=5.0)


class ExtractConfig(BaseModel):
    backend: Literal["auto", "pdfium_pdfminer", "docling", "marker", "mineru", "olmocr"] = "auto"
    ocr_engine: Literal["auto", "rapidocr", "tesseract"] = "auto"
    ocr_dpi: int = Field(default=300, ge=72, le=600)
    layout_model: str = "default"
    render_dpi_assets: int = Field(default=170, ge=72, le=400)
    routing: dict[str, str] = Field(
        default_factory=lambda: {
            "native": "docling",
            "scanned": "ocr",
            "garbled": "ocr",
        }
    )


class StructureConfig(BaseModel):
    toc_mode: Literal["auto", "outline", "toc_page", "typography", "off"] = "auto"
    detect_regions: bool = True


class ContinuityConfig(BaseModel):
    merge_threshold: float = Field(default=0.85, ge=0, le=1)
    uncertain_threshold: float = Field(default=0.5, ge=0, le=1)


class CleanConfig(BaseModel):
    dehyphenate: bool = True
    fix_spaced_letters: bool = True
    ocr_repair: Literal["off", "flag", "apply"] = "flag"
    unicode_normal: Literal["NFC", "NFKC", "NFD", "NFKD"] = "NFC"
    quotes_policy: Literal["keep", "normalize"] = "keep"


class QualityConfig(BaseModel):
    verified: float = Field(default=0.95, ge=0, le=1)
    good: float = Field(default=0.85, ge=0, le=1)
    review: float = Field(default=0.70, ge=0, le=1)
    review_gate: bool = False


class SplitConfig(BaseModel):
    by: Literal["chapter", "section", "book"] = "chapter"
    ratios: list[float] = Field(default=[0.9, 0.05, 0.05])
    seed: int = 42

    @field_validator("ratios")
    @classmethod
    def ratios_sum_to_one(cls, v: list[float]) -> list[float]:
        if abs(sum(v) - 1.0) > 0.001:
            raise ValueError("split ratios must sum to 1.0")
        return v


class DedupConfig(BaseModel):
    exact: bool = True
    near: bool = True
    jaccard: float = Field(default=0.8, ge=0, le=1)


class DatasetConfig(BaseModel):
    modes: list[str] = Field(default=["raw", "rag"])
    formats: list[str] = Field(default=["jsonl"])
    text_field: Literal["raw", "norm", "clean"] = "clean"
    tokenizer: str = "cl100k_base"
    chunk_target_tokens: int = Field(default=400, ge=50, le=8192)
    chunk_max_tokens: int = Field(default=800, ge=100, le=32768)
    split: SplitConfig = Field(default_factory=SplitConfig)
    dedup: DedupConfig = Field(default_factory=DedupConfig)


class LicenseConfig(BaseModel):
    default_status: Literal["unknown", "owned", "permissive", "copyrighted", "public_domain"] = (
        "unknown"
    )
    allow_distribution: bool = False


class AssistConfig(BaseModel):
    provider: str = "none"
    trigger_below: float = Field(default=0.7, ge=0, le=1)
    max_cost_usd: float = Field(default=5.0, ge=0)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    open_browser: bool = True


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------


class DabookConfig(BaseModel):
    name: str = "my-books"
    workspace: Path = Path("./dabook_workspace")
    profile: Literal["fast", "balanced", "accurate", "lowram"] = "balanced"

    workers: WorkersConfig = Field(default_factory=WorkersConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    extract: ExtractConfig = Field(default_factory=ExtractConfig)
    structure: StructureConfig = Field(default_factory=StructureConfig)
    continuity: ContinuityConfig = Field(default_factory=ContinuityConfig)
    clean: CleanConfig = Field(default_factory=CleanConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    dataset: DatasetConfig = Field(default_factory=DatasetConfig)
    license: LicenseConfig = Field(default_factory=LicenseConfig)
    assist: AssistConfig = Field(default_factory=AssistConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    def resolved_workspace(self, cwd: Path | None = None) -> Path:
        """Return workspace as an absolute path."""
        base = cwd or Path.cwd()
        ws = Path(self.workspace)
        if not ws.is_absolute():
            ws = base / ws
        return ws.resolve()

    def to_params_dict(self) -> dict:
        """Flat dict of output-affecting params for cache key computation."""
        return {
            "backend": self.extract.backend,
            "ocr_engine": self.extract.ocr_engine,
            "ocr_dpi": self.extract.ocr_dpi,
            "layout_model": self.extract.layout_model,
            "render_dpi_assets": self.extract.render_dpi_assets,
            "toc_mode": self.structure.toc_mode,
            "detect_regions": self.structure.detect_regions,
            "merge_threshold": self.continuity.merge_threshold,
            "uncertain_threshold": self.continuity.uncertain_threshold,
            "dehyphenate": self.clean.dehyphenate,
            "fix_spaced_letters": self.clean.fix_spaced_letters,
            "ocr_repair": self.clean.ocr_repair,
            "unicode_normal": self.clean.unicode_normal,
            "quality_verified": self.quality.verified,
            "quality_good": self.quality.good,
            "quality_review": self.quality.review,
            "modes": sorted(self.dataset.modes),
            "formats": sorted(self.dataset.formats),
            "text_field": self.dataset.text_field,
            "chunk_target_tokens": self.dataset.chunk_target_tokens,
            "chunk_max_tokens": self.dataset.chunk_max_tokens,
            "split_by": self.dataset.split.by,
            "split_ratios": self.dataset.split.ratios,
            "dedup_exact": self.dataset.dedup.exact,
            "dedup_near": self.dataset.dedup.near,
            "max_attempts": self.limits.max_attempts,
            "assist_provider": self.assist.provider,
        }


# ---------------------------------------------------------------------------
# Profile presets
# ---------------------------------------------------------------------------

PROFILES: dict[str, dict] = {
    "fast": {
        "workers": {"cpu": 8, "gpu": 1},
        "limits": {"shard_pages": 30},
        "extract": {"backend": "auto", "ocr_dpi": 200},
    },
    "balanced": {},  # defaults
    "accurate": {
        "limits": {"shard_pages": 10},
        "extract": {"backend": "docling", "ocr_dpi": 400},
        "continuity": {"merge_threshold": 0.80},
    },
    "lowram": {
        "workers": {"cpu": 2, "gpu": 0, "io": 1, "book_concurrency": 1},
        "limits": {"ram_ceiling_pct": 75, "shard_pages": 10},
    },
}
