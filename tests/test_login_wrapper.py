import pytest

from citespec_resource import login_wrapper
from citespec_resource import login_wrapper as wrapper
from citespec_resource.login_wrapper import parse_command


def test_allowed_cli_command():
    assert parse_command("citespec-resource list --json") == ["citespec-resource", "list", "--json"]


def test_empty_command_opens_menu():
    assert parse_command("") == ["citespec-resource", "menu"]


def test_job_and_file_commands_are_allowed():
    assert parse_command("citespec-resource job list abc") == [
        "citespec-resource",
        "job",
        "list",
        "abc",
    ]
    assert parse_command("citespec-resource file put abc data.bin") == [
        "citespec-resource",
        "file",
        "put",
        "abc",
        "data.bin",
    ]
    assert parse_command("citespec-resource queue list") == [
        "citespec-resource",
        "queue",
        "list",
    ]
    assert parse_command("citespec-resource tunnel abc") == [
        "citespec-resource",
        "tunnel",
        "abc",
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "bash",
        "citespec-resource; bash",
        "citespec-resource list && bash",
        "citespec-resource shell 1 | cat",
        "citespec-resource admin release 1",
        "citespec-resource config show",
        "scp local host:file",
    ],
)
def test_shell_and_unknown_commands_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_command(raw)


def test_main_forces_installed_policy_config(monkeypatch):
    captured = {}
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "citespec-resource list --json")
    monkeypatch.setenv("CITESPEC_RESOURCE_CONFIG", "/tmp/member-controlled.toml")
    monkeypatch.setattr(login_wrapper, "cli_path", lambda: "/usr/local/bin/citespec-resource")
    monkeypatch.setattr(
        login_wrapper.os,
        "execv",
        lambda path, argv: captured.update(path=path, argv=argv),
    )

    login_wrapper.main()

    assert captured["argv"][:2] == ["citespec-resource", "list"]
    assert login_wrapper.os.environ["CITESPEC_RESOURCE_CONFIG"] == (
        "/etc/citespec-resource/config.toml"
    )


def test_cli_path_prefers_interpreter_sibling(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    cli = tmp_path / "citespec-resource"
    cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(login_wrapper.sys, "executable", str(interpreter))
    assert login_wrapper.cli_path() == str(cli)


def test_cli_path_falls_back_to_path(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("", encoding="utf-8")
    fallback = tmp_path / "bin" / "citespec-resource"
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


def test_classifies_host_commands():
    assert wrapper.classify("bash")[0] == "shell"
    assert wrapper.classify("docker ps")[0] == "docker"
    assert wrapper.classify("nvidia-smi")[0] == "gpu_tool"
    assert wrapper.classify("scp file somewhere")[0] == "file_transfer"
    assert wrapper.classify("code .")[0] == "vscode"
    assert wrapper.classify("rm -rf /")[0] == "other"
    assert wrapper.classify("citespec-resource list --json")[0] == "citespec"
    assert wrapper.classify("citespec-resource bash")[0] == "unknown_subcommand"
    assert wrapper.classify("")[0] == "menu"


def test_denial_names_only_the_program():
    text = wrapper.render_denial(
        "shell",
        "bash",
        {
            "guide_url": "https://lab.example.invalid/compute",
            "apply_url": "",
            "support_label": "管理员",
            "support_contact": "admin@lab",
            "legacy": True,
        },
    )
    assert "bash" in text
    assert "使用指南：https://lab.example.invalid/compute" in text
    assert "管理员：admin@lab" in text
    # Never echoes arguments (could carry secrets).
    assert "somewhere" not in wrapper.render_denial(
        "other",
        "scp",
        {
            "guide_url": "",
            "apply_url": "",
            "support_label": "x",
            "support_contact": "",
            "legacy": True,
        },
    )


def test_denial_without_urls_is_honest():
    text = wrapper.render_denial(
        "docker",
        "docker",
        {
            "guide_url": "",
            "apply_url": "",
            "support_label": "计算资源管理员",
            "support_contact": "",
            "legacy": True,
        },
    )
    assert "请向计算资源管理员索取" in text


def test_safe_command_head_strips_control_characters():
    assert wrapper._safe_command_head("bash\x1b[31m; rm") == "bash"
    assert wrapper._safe_command_head("nvidia-smi --query=secret") == "nvidia-smi"


def test_noninteractive_usage_is_short():
    assert "机器协议" in wrapper.NON_INTERACTIVE_USAGE
    assert "菜单" in wrapper.NON_INTERACTIVE_USAGE
