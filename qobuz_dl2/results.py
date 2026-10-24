"""Explicit, immutable outcomes shared by the downloader, history, and CLI."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


@dataclass(frozen=True)
class Artifact:
    path: str
    item_type: str
    item_id: str
    source_track_id: str
    requirements_key: str
    format_id: int
    bit_depth: Optional[int] = None
    sampling_rate: Optional[float] = None


class DownloadStatus(Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class DownloadResult:
    status: DownloadStatus
    artifacts: Tuple[Artifact, ...] = ()
    reason: str = ""
    errors: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.status is DownloadStatus.COMPLETED and not self.artifacts:
            raise ValueError("Completion requires verified artifacts")
        if self.status is DownloadStatus.SKIPPED and self.artifacts:
            raise ValueError("Skipped items cannot claim completed artifacts")


@dataclass(frozen=True)
class BatchResult:
    results: Tuple[DownloadResult, ...] = ()

    @property
    def has_failures(self) -> bool:
        return any(r.status is DownloadStatus.FAILED for r in self.results)

    @property
    def artifacts(self) -> Tuple[Artifact, ...]:
        return tuple(a for r in self.results for a in r.artifacts)
