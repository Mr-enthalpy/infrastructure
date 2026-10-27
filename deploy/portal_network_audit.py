#!/usr/bin/env python3
"""Fail-closed audit of the effective nginx configuration for the Portal.

This is the acceptance-mode backend for ``verify_portal_network.sh``.  Unlike
the read-only audit, a missing vhost, an approved-CIDR mismatch, a wildcard
bind, an unsafe TLS setting, or a missing source restriction is fatal.

Only the Python standard library is used so the audit can run before the
Citespec environment is installed.  Certificate parsing and expiry checks shell
out to ``openssl`` (present on any host running nginx with TLS).
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FAIL = 0
WARN = 0

PORTAL_MARKER = "/etc/citespec-resource/portal-network-accepted"
ALLOWED_TLS_PROTOCOLS = {"TLSv1.2", "TLSv1.3"}
UNSAFE_CIPHER_TOKENS = ("NULL", "EXPORT", "RC4", "3DES", "DES-CBC", "MD5")


def fail(message: str) -> None:
    global FAIL
    FAIL += 1
    print(f"[fail] {message}")


def warn(message: str) -> None:
    global WARN
    WARN += 1
    print(f"[warn] {message}")


def note(message: str) -> None:
    print(f"[note] {message}")


def ok(message: str) -> None:
    print(f"[ok]   {message}")


@dataclass
class Directive:
    name: str
    args: list[str] = field(default_factory=list)
    block: list[Directive] = field(default_factory=list)
    raw: str = ""


def _strip_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        quote = ""
        escaped = False
        kept: list[str] = []
        for char in line:
            if escaped:
                kept.append(char)
                escaped = False
                continue
            if char == "\\":
                kept.append(char)
                escaped = True
                continue
            if quote:
                kept.append(char)
                if char == quote:
                    quote = ""
                continue
            if char in {"'", '"'}:
                quote = char
                kept.append(char)
                continue
            if char == "#":
                break
            kept.append(char)
        lines.append("".join(kept))
    return "\n".join(lines)


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
            current.append(char)
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        elif char in "{};":
            if current:
                tokens.append("".join(current))
                current = []
            tokens.append(char)
        else:
            current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_config(text: str) -> list[Directive]:
    tokens = _tokens(_strip_comments(text))
    position = 0

    def parse_block(end: str | None = None) -> list[Directive]:
        nonlocal position
        result: list[Directive] = []
        while position < len(tokens):
            token = tokens[position]
            if token == end:
                position += 1
                return result
            if token in {"}", ";"}:
                position += 1
                continue
            name = token
            position += 1
            args: list[str] = []
            while position < len(tokens) and tokens[position] not in {"{", "}", ";"}:
                args.append(_unquote(tokens[position]))
                position += 1
            directive = Directive(name=name, args=args)
            if position < len(tokens) and tokens[position] == "{":
                position += 1
                directive.block = parse_block("}")
            elif position < len(tokens) and tokens[position] == ";":
                position += 1
            result.append(directive)
        return result

    return parse_block()


def children(block: list[Directive], name: str) -> list[Directive]:
    return [item for item in block if item.name == name]


def first_value(block: list[Directive], name: str) -> str | None:
    found = children(block, name)
    if not found:
        return None
    return " ".join(found[0].args)


def _run_nginx() -> str | None:
    binary = shutil.which("nginx")
    if binary is None:
        fail("nginx is not installed; cannot inspect the effective configuration")
        return None
    test = subprocess.run([binary, "-t"], capture_output=True, text=True, check=False)
    if test.returncode != 0:
        fail(f"nginx -t failed:\n{(test.stderr or test.stdout).strip()}")
        return None
    dump = subprocess.run([binary, "-T"], capture_output=True, text=True, check=False)
    if dump.returncode != 0:
        fail(f"nginx -T failed:\n{(dump.stderr or dump.stdout).strip()}")
        return None
    return dump.stdout or ""


def _server_blocks(config: list[Directive]) -> list[Directive]:
    return [item for item in config if item.name == "server" and item.block]


def _selected(blocks: list[Directive], prefix: str) -> list[Directive]:
    selected: list[Directive] = []
    for block in blocks:
        for name in children(block.block, "server_name"):
            if any(value == prefix or value.startswith(prefix + ".") for value in name.args):
                selected.append(block)
                break
    return selected


def _is_loopback(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_loopback


def _check_listen(block: Directive, label: str, https_port: int) -> None:
    listens = children(block.block, "listen")
    if not listens:
        fail(f"{label} vhost has no listen directive")
        return
    for listen in listens:
        if not listen.args:
            fail(f"{label} vhost has an empty listen directive")
            continue
        joined = " ".join(listen.args)
        if "<" in joined or ">" in joined:
            fail(f"{label} vhost still contains an unreplaced placeholder: listen {joined}")
            continue
        target = listen.args[0]
        lowered = [arg.lower() for arg in listen.args[1:]]
        if target.startswith("unix:") or target.startswith("*:"):
            fail(f"{label} vhost binds an unsafe listen target: {target}")
            continue
        if target.isdigit():
            fail(f"{label} vhost omits a bind address (wildcard): listen {joined}")
            continue
        if target.startswith("["):
            match = re.fullmatch(r"\[([^\]]+)\]:(\d+)", target)
            if match is None:
                fail(f"{label} vhost has an invalid IPv6 listen target: {target}")
                continue
            host, port_text = match.group(1), match.group(2)
        else:
            if ":" not in target:
                fail(f"{label} vhost omits a bind address (wildcard): listen {joined}")
                continue
            host, port_text = target.rsplit(":", 1)
        if not port_text.isdigit():
            fail(f"{label} vhost has an invalid listen port: {target}")
            continue
        if int(port_text) != https_port:
            fail(f"{label} vhost must listen on TLS port {https_port}, not {port_text}")
        if "ssl" not in lowered:
            fail(f"{label} vhost listen directive is not marked ssl: listen {joined}")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            fail(f"{label} vhost does not bind an IP address: {host}")
            continue
        if address.is_unspecified or address.is_multicast:
            fail(f"{label} vhost binds a wildcard or multicast address: {target}")
        elif not (address.is_private or address.is_loopback or address.is_link_local):
            fail(f"{label} vhost binds a non-private address: {target}")
        else:
            ok(f"{label} vhost binds {target}")


def _approved_networks(values: list[str], file_path: str | None) -> list[ipaddress._BaseNetwork]:
    raw = list(values)
    if file_path:
        try:
            for line in Path(file_path).read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    raw.append(line)
        except OSError as exc:
            fail(f"cannot read approved CIDR file {file_path}: {exc}")
    networks: list[ipaddress._BaseNetwork] = []
    for value in raw:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            fail(f"invalid approved CIDR {value!r}: {exc}")
            continue
        if network.prefixlen == 0:
            fail(f"approved CIDR {value} permits the entire address space")
            continue
        networks.append(network)
    if not networks:
        fail("no approved campus/VPN CIDR was supplied (use --approved-cidr)")
    return networks


def _access_directives(block: list[Directive]) -> list[Directive]:
    return [item for item in block if item.name in {"allow", "deny"}]


def _check_access(
    block: list[Directive], label: str, approved: list[ipaddress._BaseNetwork]
) -> None:
    rules = _access_directives(block)
    if not rules:
        fail(f"{label} has no allow/deny source restriction")
        return
    allowed = 0
    last_deny_all = False
    for rule in rules:
        value = " ".join(rule.args).strip()
        if rule.name == "deny":
            last_deny_all = value == "all"
            if not last_deny_all:
                warn(f"{label} has an additional deny rule: deny {value}")
            continue
        if value == "all":
            fail(f"{label} allows every source: allow all;")
            last_deny_all = False
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            fail(f"{label} has an invalid allow rule: allow {value};")
            last_deny_all = False
            continue
        if not any(
            network.version == parent.version and network.subnet_of(parent) for parent in approved
        ):
            fail(f"{label} allows {network}, which is outside the approved campus/VPN ranges")
        else:
            ok(f"{label} allows approved source {network}")
        allowed += 1
        last_deny_all = False
    if allowed == 0:
        fail(f"{label} has no usable allow rule")
    elif not last_deny_all:
        fail(f"{label} must end its source rules with deny all;")


def _check_locations(block: Directive, label: str, approved: list[ipaddress._BaseNetwork]) -> None:
    for location in children(block.block, "location"):
        if children(location.block, "satisfy"):
            fail(f"{label} location uses satisfy and can bypass allow/deny")
        if _access_directives(location.block):
            _check_access(location.block, f"{label} location {' '.join(location.args)}", approved)


def _check_proxy(block: Directive, label: str, upstream_port: int) -> None:
    proxies = [
        directive
        for location in children(block.block, "location")
        for directive in children(location.block, "proxy_pass")
    ]
    if not proxies:
        fail(f"{label} has no proxy_pass to its loopback upstream")
        return
    expected = {f"http://127.0.0.1:{upstream_port}", f"http://[::1]:{upstream_port}"}
    for proxy in proxies:
        target = " ".join(proxy.args).strip()
        if target not in expected:
            fail(f"{label} proxies to {target}; expected http://127.0.0.1:{upstream_port}")
        else:
            ok(f"{label} proxies only to {target}")
    for location in children(block.block, "location"):
        for header in children(location.block, "proxy_set_header"):
            if not header.args:
                continue
            if header.args[0].lower() != "x-citespec-client-ip":
                continue
            value = " ".join(header.args[1:]).strip()
            if value.startswith("$http_"):
                fail(f"{label} forwards a client-supplied X-Citespec-Client-IP header")
            elif value != "$remote_addr":
                fail(f"{label} sets X-Citespec-Client-IP to {value}; expected $remote_addr")
            else:
                ok(f"{label} overwrites X-Citespec-Client-IP with $remote_addr")
    if not any(
        header.args
        and header.args[0].lower() == "x-citespec-client-ip"
        and " ".join(header.args[1:]).strip() == "$remote_addr"
        for location in children(block.block, "location")
        for header in children(location.block, "proxy_set_header")
    ):
        fail(f"{label} does not overwrite X-Citespec-Client-IP from $remote_addr")


def _check_tls(block: Directive, label: str, nginx_root: str) -> None:
    for directive in block.block:
        joined = " ".join(directive.args)
        if "<" in joined or ">" in joined:
            fail(f"{label} still contains an unreplaced placeholder: {directive.name} {joined}")
    protocols = children(block.block, "ssl_protocols")
    if not protocols:
        fail(f"{label} does not set ssl_protocols")
    else:
        values = {value for protocol in protocols for value in protocol.args}
        if values - ALLOWED_TLS_PROTOCOLS:
            fail(f"{label} enables obsolete TLS protocols: {' '.join(sorted(values))}")
        elif "TLSv1.2" not in values:
            fail(f"{label} must enable TLSv1.2")
        else:
            ok(f"{label} TLS protocols: {' '.join(sorted(values))}")
    for cipher in children(block.block, "ssl_ciphers"):
        joined = " ".join(cipher.args)
        unsafe = [token for token in UNSAFE_CIPHER_TOKENS if token in joined.upper()]
        if unsafe:
            fail(f"{label} ssl_ciphers contains unsafe tokens: {', '.join(unsafe)}")

    certificates = children(block.block, "ssl_certificate")
    keys = children(block.block, "ssl_certificate_key")
    if len(certificates) != 1 or len(keys) != 1:
        fail(f"{label} must configure exactly one ssl_certificate and one ssl_certificate_key")
        return
    cert = Path(" ".join(certificates[0].args))
    key = Path(" ".join(keys[0].args))
    # nginx resolves relative certificate paths against its prefix (normally
    # /etc/nginx), not the auditor's working directory.
    if not cert.is_absolute():
        cert = Path(nginx_root) / cert
    if not key.is_absolute():
        key = Path(nginx_root) / key
    missing = False
    for name, path in (("certificate", cert), ("key", key)):
        if not path.is_file():
            fail(f"{label} TLS {name} does not exist: {path}")
            missing = True
    if missing:
        return
    openssl = shutil.which("openssl")
    if openssl is None:
        fail("openssl is required to validate the Portal certificate")
        return

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([openssl, *args], capture_output=True, text=True, check=False)

    parsed = run("x509", "-in", str(cert), "-noout")
    if parsed.returncode != 0:
        fail(f"{label} TLS certificate is unreadable: {(parsed.stderr or '').strip()}")
    expiry = run("x509", "-in", str(cert), "-noout", "-checkend", "0")
    if expiry.returncode != 0:
        fail(f"{label} TLS certificate is expired or has not started yet")
    key_check = run("pkey", "-in", str(key), "-noout", "-check")
    if key_check.returncode != 0:
        fail(f"{label} TLS private key is invalid: {(key_check.stderr or '').strip()}")
    cert_pub = run("x509", "-in", str(cert), "-noout", "-pubkey")
    key_pub = run("pkey", "-in", str(key), "-pubout")
    if cert_pub.returncode != 0 or key_pub.returncode != 0:
        fail(f"{label} could not compare the TLS certificate and key")
    elif cert_pub.stdout.strip() != key_pub.stdout.strip():
        fail(f"{label} TLS certificate and private key do not match")
    else:
        ok(f"{label} TLS certificate and key are valid and match")


def _check_unit(marker: Path) -> tuple[bool, bool]:
    if shutil.which("systemctl") is None:
        fail("systemctl is not available; cannot inspect the Portal unit")
        return False, False
    active = (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", "citespec-resource-portal.service"],
            check=False,
        ).returncode
        == 0
    )
    enabled = (
        subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "citespec-resource-portal.service"],
            check=False,
        ).returncode
        == 0
    )
    if (active or enabled) and not marker.is_file():
        fail(
            "Portal unit is already enabled/active without the acceptance marker; "
            "disable it before running the acceptance audit"
        )
    elif active:
        ok("Portal unit is active and the acceptance marker is present")
    elif enabled:
        note("Portal unit is enabled; it will only start with the acceptance marker")
    else:
        note("Portal unit is not enabled (expected before acceptance)")
    return active, enabled


def _check_listeners(portal_active: bool, portal_port: int) -> None:
    if shutil.which("ss") is None:
        fail("ss is not available; cannot verify the Portal listener")
        return
    result = subprocess.run(["ss", "-ltn"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        fail(f"ss -ltn failed: {(result.stderr or '').strip()}")
        return
    found = False
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        address = fields[3]
        host, _, port = address.rpartition(":")
        if port != str(portal_port):
            continue
        found = True
        bare = host.strip("[]")
        if not _is_loopback(bare):
            fail(f"Portal upstream is exposed beyond loopback: {address}")
        else:
            ok(f"Portal upstream listens on loopback: {address}")
    if portal_active and not found:
        fail(f"Portal unit is active but no listener was found on port {portal_port}")
    elif not found:
        note(f"no Portal listener on port {portal_port} yet (service not started)")


def audit(args: argparse.Namespace) -> int:
    approved = _approved_networks(args.approved_cidr, args.approved_cidr_file)
    effective = _run_nginx()
    if effective is None:
        return 1
    config = parse_config(effective)
    blocks = _server_blocks(config)

    if any(
        " ".join(item.args).strip() == "any"
        for block in blocks
        for item in children(block.block, "satisfy")
    ):
        fail("an nginx server block uses 'satisfy any', which can bypass allow/deny")

    portal_blocks = _selected(blocks, "citespec-portal")
    if not portal_blocks:
        fail("no effective nginx portal vhost (server_name citespec-portal.*) was found")
    elif len(portal_blocks) > 1:
        fail(f"expected one effective portal vhost, found {len(portal_blocks)}")
    else:
        portal = portal_blocks[0]
        _check_listen(portal, "portal", args.portal_https_port)
        _check_access(portal.block, "portal", approved)
        _check_locations(portal, "portal", approved)
        _check_proxy(portal, "portal", args.portal_port)
        _check_tls(portal, "portal", args.nginx_root)

    apply_blocks = _selected(blocks, "citespec-apply")
    if not apply_blocks:
        note("no legacy apply vhost found (optional)")
    elif len(apply_blocks) > 1:
        fail(f"expected one effective apply vhost, found {len(apply_blocks)}")
    else:
        apply_block = apply_blocks[0]
        _check_listen(apply_block, "apply", args.portal_https_port)
        _check_access(apply_block.block, "apply", approved)
        _check_locations(apply_block, "apply", approved)
        _check_proxy(apply_block, "apply", args.apply_port)
        _check_tls(apply_block, "apply", args.nginx_root)

    portal_active, _ = _check_unit(Path(args.marker))
    _check_listeners(portal_active, args.portal_port)
    return FAIL


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed audit of the effective nginx Portal configuration."
    )
    parser.add_argument("--nginx-root", default="/etc/nginx")
    parser.add_argument("--portal-port", type=int, default=8770)
    parser.add_argument("--apply-port", type=int, default=8765)
    parser.add_argument("--portal-https-port", type=int, default=443)
    parser.add_argument(
        "--approved-cidr",
        action="append",
        default=[],
        help="Approved campus/VPN source (repeatable).",
    )
    parser.add_argument("--approved-cidr-file")
    parser.add_argument("--marker", default=PORTAL_MARKER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    failures = audit(args)
    print(f"\nfailures: {failures}, warnings: {WARN}")
    if failures:
        print("portal network acceptance audit found blocking problems.")
        return 1
    print("portal network acceptance audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
