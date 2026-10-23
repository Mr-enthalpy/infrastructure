import os
from pathlib import Path

import pytest

from udc_resource.broker import workspace_io
from udc_resource.errors import BadRequest, Forbidden

requires_linux = pytest.mark.skipif(
    not workspace_io.supported(),
    reason="descriptor-relative file operations require Linux",
)


@pytest.mark.skipif(os.name != "posix", reason="production brokers run on Linux")
def test_descriptor_relative_support_is_available():
    # Do not turn a capability regression into skipped Linux tests.
    assert workspace_io.supported()


def test_relative_parts_rejects_traversal():
    for value in ("/etc/passwd", "../secret", "a/../../secret", "a//b", "", "/workspace"):
        with pytest.raises((BadRequest, Forbidden)):
            workspace_io.relative_parts(value)
    assert workspace_io.relative_parts("/workspace/a/b.txt") == ("a", "b.txt")
    assert workspace_io.relative_parts("a/b.txt") == ("a", "b.txt")


@requires_linux
def test_ensure_workspace_creates_private_directory(tmp_path: Path):
    workspace = tmp_path / "workspaces" / "alice"
    workspace_io.ensure_workspace(workspace)
    assert workspace.is_dir()
    assert workspace.stat().st_mode & 0o777 == 0o700


@requires_linux
def test_ensure_workspace_rejects_a_symlink_without_chmodding_target(tmp_path: Path):
    root = tmp_path / "workspaces"
    root.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    target.chmod(0o755)
    link = root / "alice"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(Forbidden):
        workspace_io.ensure_workspace(link)
    assert target.stat().st_mode & 0o777 == 0o755


@requires_linux
def test_atomic_upload_creates_directories_and_replaces(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    destination = workspace / "nested" / "model.bin"
    destination.parent.mkdir()
    destination.write_bytes(b"old")

    upload = workspace_io.open_upload(workspace, ("nested", "model.bin"))
    os.write(upload.fd, b"new")
    upload.commit()

    assert destination.read_bytes() == b"new"
    assert not list(destination.parent.glob(".udc-upload-*"))
    assert workspace_io.regular_file_size(workspace, ("nested", "model.bin")) == 3


@requires_linux
def test_upload_abort_cleans_temporary_file(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    upload = workspace_io.open_upload(workspace, ("new", "file.bin"))
    os.write(upload.fd, b"partial")
    upload.abort()
    assert not (workspace / "new" / "file.bin").exists()
    assert not list((workspace / "new").glob(".udc-upload-*"))


@requires_linux
def test_read_and_upload_reject_symlinks(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside)
    (workspace / "dir-link").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(Forbidden):
        workspace_io.open_read_file(workspace, ("link",))
    with pytest.raises(Forbidden):
        workspace_io.open_read_file(workspace, ("dir-link", "outside.txt"))
    with pytest.raises(Forbidden):
        workspace_io.open_upload(workspace, ("dir-link", "new.txt"))


@requires_linux
def test_read_rejects_fifo_and_devices(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.mkfifo(workspace / "pipe")
    with pytest.raises(Forbidden):
        workspace_io.open_read_file(workspace, ("pipe",))


@requires_linux
def test_job_logs_are_regular_and_symlink_safe(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace_io.ensure_job_log(workspace, "train")
    log = workspace / ".udc-logs" / "train.log"
    assert log.is_file()
    log.write_text("line 1\nline 2\n", encoding="utf-8")
    assert workspace_io.regular_file_size(workspace, (".udc-logs", "train.log")) == 14
    assert workspace_io.tail_file(workspace, (".udc-logs", "train.log"), 1) == "line 2\n"

    with pytest.raises(BadRequest):
        workspace_io.ensure_job_log(workspace, "../escape")

    fifo_workspace = tmp_path / "fifo-workspace"
    fifo_workspace.mkdir()
    (fifo_workspace / ".udc-logs").mkdir()
    os.mkfifo(fifo_workspace / ".udc-logs" / "train.log")
    with pytest.raises(Forbidden):
        workspace_io.ensure_job_log(fifo_workspace, "train")

    linked_workspace = tmp_path / "linked-workspace"
    linked_workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (linked_workspace / ".udc-logs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Forbidden):
        workspace_io.ensure_job_log(linked_workspace, "train")
