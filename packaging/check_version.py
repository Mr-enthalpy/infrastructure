"""Fail a release when the tag, pyproject version and package version disagree."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

from citespec_resource import __version__

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    tag_version = args.tag.removeprefix("v")
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    project_version = str(pyproject.get("project", {}).get("version", ""))
    lock_version = _lock_version()
    if not (tag_version == project_version == __version__ == lock_version):
        raise SystemExit(
            "version mismatch: "
            f"tag={tag_version!r} pyproject={project_version!r} "
            f"package={__version__!r} uv.lock={lock_version!r}"
        )
    print(f"version ok: {tag_version}")
    return 0


def _lock_version() -> str:
    lock_path = ROOT / "uv.lock"
    if not lock_path.is_file():
        return ""
    with lock_path.open("rb") as handle:
        lock = tomllib.load(handle)
    for package in lock.get("package", []):
        if isinstance(package, dict) and package.get("name") == "citespec-resource":
            return str(package.get("version", ""))
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
