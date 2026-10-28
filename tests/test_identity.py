"""Portal capability must be bound to the service account, not a group."""

from __future__ import annotations

import types

import pytest

from citespec_resource import identity as identity_module
from citespec_resource.identity import (
    IdentityResolver,
    portal_account_issues,
    portal_group_extra_members,
    resolve_portal_account,
)

PORTAL_UID = 999
PORTAL_GID = 999
HUMAN_UID = 1001
HUMAN_GID = 1001
CLI_GID = 996


class _Account(types.SimpleNamespace):
    pass


class _Group(types.SimpleNamespace):
    pass


class _FakePwd:
    def __init__(self, accounts: dict[int, _Account], by_name: dict[str, _Account]) -> None:
        self._accounts = accounts
        self._by_name = by_name

    def getpwuid(self, uid: int) -> _Account:
        return self._accounts[uid]

    def getpwnam(self, name: str) -> _Account:
        return self._by_name[name]


class _FakeGrp:
    def __init__(self, groups: dict[int, _Group], by_name: dict[str, _Group]) -> None:
        self._groups = groups
        self._by_name = by_name

    def getgrgid(self, gid: int) -> _Group:
        return self._groups[gid]

    def getgrnam(self, name: str) -> _Group:
        return self._by_name[name]


@pytest.fixture
def fake_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    portal = _Account(
        pw_uid=PORTAL_UID,
        pw_gid=PORTAL_GID,
        pw_name="citespec-portal",
        pw_shell="/usr/sbin/nologin",
        pw_dir="/var/lib/citespec-resource-portal",
    )
    human = _Account(
        pw_uid=HUMAN_UID,
        pw_gid=HUMAN_GID,
        pw_name="alice",
        pw_shell="/bin/bash",
        pw_dir="/home/alice",
    )
    portal_group = _Group(gr_name="citespec-portal", gr_gid=PORTAL_GID, gr_mem=[])
    cli_group = _Group(gr_name="citespec-cli", gr_gid=CLI_GID, gr_mem=["citespec-portal", "alice"])
    admin_group = _Group(gr_name="citespec-admins", gr_gid=995, gr_mem=[])
    accounts = {PORTAL_UID: portal, HUMAN_UID: human}
    by_name = {"citespec-portal": portal, "alice": human}
    groups = {PORTAL_GID: portal_group, CLI_GID: cli_group, 995: admin_group}
    by_group_name = {
        "citespec-portal": portal_group,
        "citespec-cli": cli_group,
        "citespec-admins": admin_group,
    }
    monkeypatch.setattr(identity_module, "pwd", _FakePwd(accounts, by_name))
    monkeypatch.setattr(identity_module, "grp", _FakeGrp(groups, by_group_name))
    membership = {
        "citespec-portal": [PORTAL_GID, CLI_GID],
        "alice": [HUMAN_GID, CLI_GID, PORTAL_GID],
    }
    monkeypatch.setattr(
        identity_module.os,
        "getgrouplist",
        lambda name, gid: membership.get(name, [gid]),
        raising=False,
    )


def test_portal_account_resolves_to_uid(fake_accounts: None) -> None:
    assert resolve_portal_account("citespec-portal") == (PORTAL_UID, "citespec-portal")
    assert portal_account_issues("citespec-portal") == ()


def test_portal_account_rejects_an_interactive_shell(fake_accounts: None) -> None:
    account = identity_module.pwd.getpwnam("citespec-portal")  # type: ignore[union-attr]
    account.pw_shell = "/bin/bash"

    issues = portal_account_issues("citespec-portal")

    assert any("interactive" in issue for issue in issues)
    assert resolve_portal_account("citespec-portal") is None


def test_portal_account_rejects_a_non_system_uid(fake_accounts: None) -> None:
    account = identity_module.pwd.getpwnam("citespec-portal")  # type: ignore[union-attr]
    account.pw_uid = 4242

    issues = portal_account_issues("citespec-portal")

    assert any("system range" in issue for issue in issues)
    assert resolve_portal_account("citespec-portal") is None


def test_portal_account_rejects_a_foreign_primary_group(fake_accounts: None) -> None:
    account = identity_module.pwd.getpwnam("citespec-portal")  # type: ignore[union-attr]
    account.pw_gid = HUMAN_GID

    issues = portal_account_issues("citespec-portal")

    assert any("primary group" in issue for issue in issues)


def test_portal_account_rejects_privileged_supplementary_groups(
    fake_accounts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        identity_module.os,
        "getgrouplist",
        lambda name, gid: [PORTAL_GID, CLI_GID, 994] if name == "citespec-portal" else [gid],
        raising=False,
    )
    original = identity_module.grp.getgrgid  # type: ignore[union-attr]

    def getgrgid(gid: int) -> _Group:
        if gid == 994:
            return _Group(gr_name="docker", gr_gid=994, gr_mem=[])
        return original(gid)

    monkeypatch.setattr(identity_module.grp, "getgrgid", getgrgid)

    issues = portal_account_issues("citespec-portal")

    assert any("privileged group" in issue for issue in issues)


def test_portal_account_rejects_unexpected_supplementary_groups(
    fake_accounts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        identity_module.os,
        "getgrouplist",
        lambda name, gid: [PORTAL_GID, CLI_GID, 997] if name == "citespec-portal" else [gid],
        raising=False,
    )
    original = identity_module.grp.getgrgid  # type: ignore[union-attr]

    def getgrgid(gid: int) -> _Group:
        if gid == 997:
            return _Group(gr_name="users", gr_gid=997, gr_mem=[])
        return original(gid)

    monkeypatch.setattr(identity_module.grp, "getgrgid", getgrgid)

    issues = portal_account_issues("citespec-portal")

    assert any("unexpected supplementary group" in issue for issue in issues)


def test_portal_capability_requires_matching_uid(fake_accounts: None) -> None:
    resolver = IdentityResolver(
        "citespec-admins",
        portal_uid=PORTAL_UID,
        portal_username="citespec-portal",
    )

    human = resolver.resolve(HUMAN_UID, HUMAN_GID)

    assert human.is_portal is False
    assert human.is_admin is False


def test_portal_capability_accepts_service_account(fake_accounts: None) -> None:
    resolver = IdentityResolver(
        "citespec-admins",
        portal_uid=PORTAL_UID,
        portal_username="citespec-portal",
    )

    portal = resolver.resolve(PORTAL_UID, PORTAL_GID)

    assert portal.is_portal is True


def test_portal_capability_rejects_uid_reuse(fake_accounts: None) -> None:
    resolver = IdentityResolver(
        "citespec-admins",
        portal_uid=PORTAL_UID,
        portal_username="different-account",
    )

    portal = resolver.resolve(PORTAL_UID, PORTAL_GID)

    assert portal.is_portal is False


def test_portal_capability_disabled_without_resolved_account(fake_accounts: None) -> None:
    resolver = IdentityResolver("citespec-admins")

    portal = resolver.resolve(PORTAL_UID, PORTAL_GID)

    assert portal.is_portal is False


def test_portal_group_drift_reports_human_members(fake_accounts: None) -> None:
    assert portal_group_extra_members("citespec-portal", "citespec-portal") == ()


def test_portal_group_drift_detects_supplementary_member(
    fake_accounts: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_grp = identity_module.grp
    original = fake_grp.getgrnam  # type: ignore[union-attr]

    def getgrnam(name: str) -> _Group:
        info = original(name)
        if name == "citespec-portal":
            return _Group(gr_name=info.gr_name, gr_gid=info.gr_gid, gr_mem=["alice"])
        return info

    monkeypatch.setattr(fake_grp, "getgrnam", getgrnam)

    assert portal_group_extra_members("citespec-portal", "citespec-portal") == ("alice",)
