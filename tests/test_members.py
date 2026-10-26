import os
from pathlib import Path

import pytest

from citespec_resource.members import load_members, public_member_data


def test_missing_directory_is_empty(tmp_path: Path):
    assert load_members(tmp_path / "missing.toml") == {}


def test_public_member_directory_filters_private_fields(tmp_path: Path):
    path = tmp_path / "members.toml"
    path.write_text(
        """
[members.alice]
display_name = "Alice Zhang"
nickname = "alice"
public_contact = "alice@example.invalid"
private_phone = "+1-555-0100"

[members.bob]
nickname = "bob"
""",
        encoding="utf-8",
    )
    members = load_members(path)
    assert members["alice"].display_name == "Alice Zhang"
    assert members["alice"].public_contact == "alice@example.invalid"
    assert public_member_data(members, "alice") == {
        "display_name": "Alice Zhang",
        "nickname": "alice",
        "public_contact": "alice@example.invalid",
    }
    assert "private_phone" not in public_member_data(members, "alice")
    assert public_member_data(members, "bob") == {"nickname": "bob"}
    assert public_member_data(members, "unknown") == {}


def test_example_member_directory_is_valid():
    example = Path(__file__).resolve().parents[1] / "lab" / "members.example.toml"
    members = load_members(example)
    assert members["alice"].display_name == "Alice Example"
    assert public_member_data(members, "bob") == {
        "display_name": "Bob Example",
        "nickname": "bob",
    }


@pytest.mark.skipif(os.name != "posix", reason="O_NOFOLLOW is Linux-only")
def test_member_directory_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "real.toml"
    target.write_text('[members.alice]\nnickname = "alice"\n', encoding="utf-8")
    link = tmp_path / "members.toml"
    link.symlink_to(target)
    with pytest.raises(OSError):
        load_members(link)


def test_invalid_member_username_is_rejected(tmp_path: Path):
    path = tmp_path / "members.toml"
    path.write_text(
        """
[members."bad/name"]
display_name = "bad"
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_members(path)


def test_member_values_must_be_strings(tmp_path: Path):
    path = tmp_path / "members.toml"
    path.write_text(
        """
[members.alice]
display_name = 42
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_members(path)
