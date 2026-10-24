"""Reject accidental workspace state in wheel/sdist archives."""

import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


def main():
    directory = Path(sys.argv[1])
    wheels, sources = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    assert len(wheels) == len(sources) == 1, "Expected exactly one wheel and sdist"
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        assert all(name.startswith(("qobuz_dl2/", "qobuz_dl2-2.0.0.dist-info/")) for name in names)
        assert "qobuz_dl2/storage.py" in names and "qobuz_dl2/config.py" in names
    allowed = {
        "qobuz_dl2",
        "tests",
        "scripts",
        "benchmarks",
        ".github",
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "uv.lock",
        "PKG-INFO",
        ".gitignore",
    }
    with tarfile.open(sources[0]) as source:
        for member in source.getmembers():
            parts = PurePosixPath(member.name).parts
            assert len(parts) > 1 and parts[1] in allowed, f"Unexpected source archive member: {member.name}"
            assert not member.issym() and not member.islnk(), f"Archive link: {member.name}"
            assert not {"__pycache__", ".superpowers", ".venv", "config.ini"}.intersection(parts), member.name
            assert not member.name.endswith((".db", ".log", ".pyc", ".tmp")), member.name
    print(f"Verified {wheels[0].name} and {sources[0].name}: no runtime/workspace state")


if __name__ == "__main__":
    main()
