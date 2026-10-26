"""Regression guard for the one-time legacy -> Citespec naming cutover."""

from __future__ import annotations

import tomllib
from pathlib import Path

LEGACY = "".join(("u", "d", "c"))
ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


def _source_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.endswith((".egg-info", ".pyc", ".pyo", ".local.md")):
            continue
        if ".local." in path.name:
            continue
        files.append(path)
    return files


def test_no_legacy_identity_in_paths_or_text() -> None:
    findings: list[str] = []
    for path in _source_files():
        if LEGACY in path.name.lower():
            findings.append(f"path: {path.relative_to(ROOT)}")
            continue
        try:
            if path.stat().st_size > 5_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if LEGACY in line.lower():
                findings.append(f"{path.relative_to(ROOT)}:{line_no}")
                break
    assert not findings, "legacy identity references remain:\n" + "\n".join(findings)


def test_canonical_project_and_entry_points() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["name"] == "citespec-resource"
    assert data["project"]["scripts"]["citespec-resource"] == ("citespec_resource.cli:app")
    assert data["project"]["scripts"]["citespec-resource-login"] == (
        "citespec_resource.login_wrapper:main"
    )
    assert (ROOT / "src" / "citespec_resource").is_dir()
    assert not (ROOT / "src" / f"{LEGACY}_resource").exists()
