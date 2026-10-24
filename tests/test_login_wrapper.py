import pytest

from udc_resource import login_wrapper
from udc_resource.login_wrapper import parse_command


def test_allowed_cli_command():
    assert parse_command("udc-resource list --json") == ["udc-resource", "list", "--json"]


def test_empty_command_opens_menu():
    assert parse_command("") == ["udc-resource", "menu"]


def test_job_and_file_commands_are_allowed():
    assert parse_command("udc-resource job list abc") == [
        "udc-resource",
        "job",
        "list",
        "abc",
    ]
    assert parse_command("udc-resource file put abc data.bin") == [
        "udc-resource",
        "file",
        "put",
        "abc",
        "data.bin",
    ]
    assert parse_command("udc-resource queue list") == [
        "udc-resource",
        "queue",
        "list",
    ]
    assert parse_command("udc-resource tunnel abc") == [
        "udc-resource",
        "tunnel",
        "abc",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "bash",
        "udc-resource; bash",
        "udc-resource list && bash",
        "udc-resource shell 1 | cat",
        "udc-resource admin release 1",
        "udc-resource config show",
        "scp local host:file",
    ],
)
def test_shell_and_unknown_commands_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_command(raw)


def test_main_forces_installed_policy_config(monkeypatch):
    captured = {}
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "udc-resource list --json")
    monkeypatch.setenv("UDC_RESOURCE_CONFIG", "/tmp/member-controlled.toml")
    monkeypatch.setattr(login_wrapper, "cli_path", lambda: "/usr/local/bin/udc-resource")
    monkeypatch.setattr(
        login_wrapper.os,
        "execv",
        lambda path, argv: captured.update(path=path, argv=argv),
    )

    login_wrapper.main()

    assert captured["argv"][:2] == ["udc-resource", "list"]
    assert login_wrapper.os.environ["UDC_RESOURCE_CONFIG"] == ("/etc/udc-resource/config.toml")


def test_cli_path_prefers_interpreter_sibling(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    cli = tmp_path / "udc-resource"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(login_wrapper.sys, "executable", str(interpreter))
    assert login_wrapper.cli_path() == str(cli)


def test_cli_path_falls_back_to_path(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    fallback = tmp_path / "bin" / "udc-resource"
    fallback.parent.mkdir()
    fallback.write_text("", encoding="utf-8")
    monkeypatch.setattr(login_wrapper.sys, "executable", str(interpreter))
    monkeypatch.setattr(login_wrapper.shutil, "which", lambda name: str(fallback))
    assert login_wrapper.cli_path() == str(fallback)


def test_cli_path_raises_when_missing(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    monkeypatch.setattr(login_wrapper.sys, "executable", str(interpreter))
    monkeypatch.setattr(login_wrapper.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        login_wrapper.cli_path()
