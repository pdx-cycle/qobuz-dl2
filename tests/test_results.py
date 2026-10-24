import importlib.util


def test_completion_requires_verified_artifacts():
    assert importlib.util.find_spec("qobuz_dl2.results") is not None, "Explicit outcome contract missing"
    import pytest

    from qobuz_dl2.results import DownloadResult, DownloadStatus

    with pytest.raises(ValueError):
        DownloadResult(DownloadStatus.COMPLETED)
    assert not DownloadResult(DownloadStatus.SKIPPED, reason="policy").artifacts
