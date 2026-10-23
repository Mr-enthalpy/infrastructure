#!/usr/bin/env python3
"""Destructive active-mode acceptance checks for an approved test window.

Run this on the server as root, with SSH aliases for two member accounts. It
temporarily creates allocations, a test GPU holder container, restarts the
broker, and adjusts one test allocation's expires_at in SQLite. It refuses to
run without --yes and the explicit destructive-test flags.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SSH_DEFAULT_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
]


class AcceptanceError(RuntimeError):
    pass


def ssh(alias: str, command: str, *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ssh", *SSH_DEFAULT_OPTIONS, alias, command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ssh_cli(
    alias: str,
    args: list[str],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    return ssh_cli_raw(alias, [*args, "--json"], timeout=timeout)


def ssh_cli_raw(
    alias: str,
    args: list[str],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    command = shlex.join(["udc-resource", *args])
    result = ssh(alias, command, timeout=timeout)
    text = result.stdout.strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AcceptanceError(
            f"SSH CLI returned invalid JSON for {alias}: {text or result.stderr}"
        ) from exc
    if not isinstance(payload, dict):
        raise AcceptanceError(f"SSH CLI returned a non-object for {alias}")
    return payload


def local(command: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def require_ok(payload: dict[str, Any], context: str) -> dict[str, Any]:
    if not payload.get("ok"):
        error = payload.get("error") or {}
        raise AcceptanceError(
            f"{context}: {error.get('code', 'error')}: {error.get('message', 'failed')}"
        )
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def wait_until(predicate: Any, timeout: float, interval: float, description: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AcceptanceError(f"timed out waiting for {description} (last={last!r})")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def container_running(container_id: str) -> bool:
    result = local(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def container_exists(container_id: str) -> bool:
    return local(["docker", "inspect", container_id]).returncode == 0


class Acceptance:
    def __init__(self, args: argparse.Namespace, config: dict[str, Any]) -> None:
        self.args = args
        self.config = config
        self.member_a = args.member_a
        self.member_b = args.member_b
        self.member_a_user = args.member_a_user
        self.member_b_user = args.member_b_user
        self.gpu_index = args.gpu
        self.image = args.image or str(config.get("image", {}).get("base", "udc-base:2026.10"))
        self.db_path = Path(
            str(config.get("server", {}).get("state_db", "/var/lib/udc-resource/state.db"))
        )
        self.socket_path = Path(
            str(config.get("server", {}).get("broker_socket", "/run/udc-resource/broker.sock"))
        )
        self.holder_name = f"udc-accept-holder-{os.getpid()}"
        self.allocations: list[tuple[str, str]] = []
        self.holder_container = ""

    def cleanup(self) -> None:
        for alias, allocation_id in reversed(self.allocations):
            try:
                ssh_cli(alias, ["release", allocation_id], timeout=30)
            except Exception as exc:
                print(f"[warn] cleanup release {allocation_id}: {exc}", file=sys.stderr)
        if self.holder_container:
            local(["docker", "rm", "-f", self.holder_container], timeout=30)
        elif self.holder_name:
            local(["docker", "rm", "-f", self.holder_name], timeout=30)

    def gpu(self) -> dict[str, Any]:
        data = require_ok(
            ssh_cli(self.member_a, ["list"]),
            "list",
        )
        gpus = data.get("gpus") or []
        for gpu in gpus:
            if int(gpu.get("index", -1)) == self.gpu_index:
                return gpu
        raise AcceptanceError(f"GPU {self.gpu_index} not found")

    def require_free_gpu(self) -> dict[str, Any]:
        gpu = self.gpu()
        if gpu.get("state") != "free":
            raise AcceptanceError(
                f"test GPU {self.gpu_index} is not free: {gpu.get('state')} "
                f"({gpu.get('state_reason')})"
            )
        return gpu

    def test_real_ssh(self) -> None:
        data = require_ok(ssh_cli(self.member_a, ["list"]), "member CLI")
        if data.get("mode") != "active":
            raise AcceptanceError(f"broker is not in active mode: {data.get('mode')}")
        checks = [
            ("host shell", "bash -lc 'echo host-shell'"),
            ("docker", "docker ps"),
            ("gpu device", "test -r /dev/nvidia0"),
        ]
        for label, command in checks:
            result = ssh(self.member_a, command, timeout=30)
            if result.returncode == 0:
                raise AcceptanceError(f"member can run {label}: {command}")
        for user in (self.member_a_user, self.member_b_user):
            if not user:
                continue
            for label, command in (
                ("docker (local account)", "docker ps"),
                ("GPU device (local account)", "test -r /dev/nvidia0"),
            ):
                result = local(["su", "-s", "/bin/bash", user, "-c", command], timeout=30)
                if result.returncode == 0:
                    raise AcceptanceError(f"{user} can run {label}: {command}")
        directory = self.socket_path.parent
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode != 0o711:
            raise AcceptanceError(f"socket parent mode is {mode:o}, expected 711 (root:root)")
        if not self.member_a_user:
            print(
                "[warn] no --member-a-user supplied; direct host Docker/GPU denial was not tested"
            )
        print("[pass] real SSH: CLI works; arbitrary commands denied; socket parent 711")

    def test_concurrent_apply(self) -> None:
        gpu = self.require_free_gpu()
        args = [
            "apply",
            "--gpus",
            str(self.gpu_index),
            "--cpus",
            "2",
            "--memory",
            "4",
            "--hours",
            "1",
            "--note",
            "acceptance concurrency",
        ]

        def attempt(alias: str) -> tuple[str, dict[str, Any]]:
            return alias, ssh_cli(alias, args, timeout=60)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [self.member_a, self.member_b]))
        successes = [(alias, payload) for alias, payload in results if payload.get("ok")]
        for alias, payload in successes:
            data = require_ok(payload, "concurrent successful apply")
            self.allocations.append((alias, str(data["id"])))
        if len(successes) != 1:
            raise AcceptanceError(f"expected exactly one concurrent apply success, got {results}")
        failures = [payload for _alias, payload in results if not payload.get("ok")]
        if not failures or (failures[0].get("error") or {}).get("code") != "gpu_busy":
            raise AcceptanceError(f"expected the loser to receive gpu_busy, got {results}")
        winner_alias, winner = successes[0]
        winner_data = require_ok(winner, "concurrent winner")
        allocation_id = str(winner_data["id"])
        if not container_running(str(winner_data["container_id"])):
            raise AcceptanceError("winning allocation container is not running")
        require_ok(
            ssh_cli(winner_alias, ["release", allocation_id]),
            "release winner",
        )
        self.allocations.remove((winner_alias, allocation_id))
        wait_until(
            lambda: self.gpu().get("state") == "free",
            30,
            1,
            "GPU to become free after release",
        )
        print(f"[pass] concurrent apply: one winner ({winner_alias}); GPU {gpu['index']} released")

    def test_unmanaged_gpu_holder(self) -> None:
        self.require_free_gpu()
        if not shutil.which("docker"):
            raise AcceptanceError("docker is required for the holder test")
        image_check = local(["docker", "image", "inspect", self.image])
        if image_check.returncode != 0:
            raise AcceptanceError(f"test image is not available: {self.image}")
        run = local(
            [
                "docker",
                "run",
                "-d",
                "--name",
                self.holder_name,
                "--gpus",
                f"device={self.gpu_index}",
                "--entrypoint",
                "sleep",
                self.image,
                "infinity",
            ]
        )
        if run.returncode != 0:
            raise AcceptanceError(f"could not start test holder: {run.stderr.strip()}")
        self.holder_container = run.stdout.strip()
        try:
            if not container_running(self.holder_container):
                raise AcceptanceError("test holder is not running")
            response = ssh_cli(
                self.member_a,
                [
                    "apply",
                    "--gpus",
                    str(self.gpu_index),
                    "--cpus",
                    "2",
                    "--memory",
                    "4",
                    "--hours",
                    "1",
                    "--note",
                    "acceptance holder rejection",
                ],
            )
            if response.get("ok"):
                data = require_ok(response, "unexpected holder apply")
                self.allocations.append((self.member_a, str(data["id"])))
                raise AcceptanceError("apply succeeded while an unmanaged container held the GPU")
            error = response.get("error") or {}
            if error.get("code") != "gpu_busy":
                raise AcceptanceError(f"expected gpu_busy, got {error}")
            print("[pass] unmanaged GPU holder rejected apply with gpu_busy")
        finally:
            local(["docker", "rm", "-f", self.holder_name], timeout=30)
            self.holder_container = ""
        wait_until(
            lambda: self.gpu().get("state") == "free",
            30,
            1,
            "GPU to become free after holder cleanup",
        )

    def test_file_transfer(self, allocation_id: str) -> None:
        remote = f"acceptance/transfer-{os.getpid()}.bin"
        payload = b"udc-transfer\x00" + os.urandom(32)
        put = subprocess.run(
            [
                "ssh",
                *SSH_DEFAULT_OPTIONS,
                self.member_a,
                shlex.join(
                    [
                        "udc-resource",
                        "file",
                        "put",
                        allocation_id,
                        f"/workspace/{remote}",
                    ]
                ),
            ],
            input=payload,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if put.returncode != 0:
            raise AcceptanceError(
                "file put failed: " + (put.stderr or put.stdout).decode("utf-8", errors="replace")
            )
        try:
            ack = json.loads(put.stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise AcceptanceError(f"file put returned invalid JSON: {put.stdout!r}") from exc
        if not ack.get("ok"):
            raise AcceptanceError(f"file put was rejected: {ack}")

        get = subprocess.run(
            [
                "ssh",
                *SSH_DEFAULT_OPTIONS,
                self.member_a,
                shlex.join(["udc-resource", "file", "get", allocation_id, remote]),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if get.returncode != 0:
            raise AcceptanceError(
                "file get failed: " + (get.stderr or get.stdout).decode("utf-8", errors="replace")
            )
        if get.stdout != payload:
            raise AcceptanceError("file get returned different bytes")

        escape = subprocess.run(
            [
                "ssh",
                *SSH_DEFAULT_OPTIONS,
                self.member_a,
                shlex.join(
                    [
                        "udc-resource",
                        "file",
                        "put",
                        allocation_id,
                        "../escape",
                    ]
                ),
            ],
            input=b"must-not-write",
            capture_output=True,
            timeout=60,
            check=False,
        )
        if escape.returncode == 0:
            raise AcceptanceError("file put accepted a workspace escape path")
        detail = (escape.stdout + escape.stderr).decode("utf-8", errors="replace").lower()
        if "forbidden" not in detail:
            raise AcceptanceError(f"file put escape was not rejected as forbidden: {detail}")
        print("[pass] restricted file put/get works and rejects traversal")

    def test_expiry_restart_disconnect(self) -> None:
        self.require_free_gpu()
        if not self.args.allow_db_time_travel:
            raise AcceptanceError("--allow-db-time-travel is required for expiry test")
        allocation_id = ""
        bystander_id = ""
        container_id = ""
        allocation_data = require_ok(
            ssh_cli(
                self.member_a,
                [
                    "apply",
                    "--gpus",
                    str(self.gpu_index),
                    "--cpus",
                    "2",
                    "--memory",
                    "4",
                    "--hours",
                    "1",
                    "--note",
                    "acceptance expiry",
                ],
            ),
            "expiry allocation",
        )
        allocation_id = str(allocation_data["id"])
        container_id = str(allocation_data["container_id"])
        workspace = Path(str(allocation_data["workspace"]))
        self.allocations.append((self.member_a, allocation_id))
        bystander = require_ok(
            ssh_cli(
                self.member_b,
                [
                    "apply",
                    "--cpus",
                    "1",
                    "--memory",
                    "2",
                    "--hours",
                    "1",
                    "--note",
                    "acceptance bystander",
                ],
            ),
            "bystander allocation",
        )
        bystander_id = str(bystander["id"])
        self.allocations.append((self.member_b, bystander_id))
        try:
            shell = subprocess.Popen(
                [
                    "ssh",
                    "-tt",
                    *SSH_DEFAULT_OPTIONS,
                    self.member_a,
                    f"udc-resource shell {allocation_id}",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                assert shell.stdin is not None
                shell.stdin.write("echo acceptance-marker > /workspace/acceptance-marker\n")
                shell.stdin.flush()
                time.sleep(3)
            finally:
                shell.terminate()
                try:
                    shell.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    shell.kill()
            if not container_running(container_id):
                raise AcceptanceError("container stopped after SSH disconnect")
            marker = workspace / "acceptance-marker"
            if not marker.is_file():
                raise AcceptanceError(f"workspace marker is missing: {marker}")
            self.test_file_transfer(allocation_id)

            job_name = f"accept-{os.getpid()}"
            try:
                started = require_ok(
                    ssh_cli_raw(
                        self.member_a,
                        [
                            "job",
                            "start",
                            allocation_id,
                            "--name",
                            job_name,
                            "--json",
                            "--",
                            "bash",
                            "-lc",
                            "echo job-started; sleep 900",
                        ],
                    ),
                    "job start",
                )
                log_path = Path(str(started["log"]))
                if not log_path.is_file():
                    raise AcceptanceError(f"job log was not created: {log_path}")
                wait_until(
                    lambda: "job-started" in log_path.read_text(errors="replace"),
                    30,
                    1,
                    "job log content",
                )
                listed = require_ok(
                    ssh_cli(self.member_a, ["job", "list", allocation_id]),
                    "job list before disconnect",
                )
                if not any(item.get("name") == job_name for item in listed.get("jobs", [])):
                    raise AcceptanceError("job is missing before SSH disconnect")

                for attempt in range(2):
                    attach = subprocess.Popen(
                        [
                            "ssh",
                            "-tt",
                            *SSH_DEFAULT_OPTIONS,
                            self.member_a,
                            shlex.join(
                                [
                                    "udc-resource",
                                    "job",
                                    "attach",
                                    allocation_id,
                                    "--name",
                                    job_name,
                                ]
                            ),
                        ],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        text=True,
                    )
                    try:
                        time.sleep(2)
                    finally:
                        attach.terminate()
                        try:
                            attach.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            attach.kill()
                    listed = require_ok(
                        ssh_cli(self.member_a, ["job", "list", allocation_id]),
                        f"job list after attach disconnect {attempt + 1}",
                    )
                    if not any(item.get("name") == job_name for item in listed.get("jobs", [])):
                        raise AcceptanceError(f"job died after SSH attach disconnect {attempt + 1}")
                logs = require_ok(
                    ssh_cli(
                        self.member_a,
                        ["job", "logs", allocation_id, "--name", job_name],
                    ),
                    "job logs before restart",
                )
                if "job-started" not in str(logs.get("logs", "")):
                    raise AcceptanceError("job log did not survive attach disconnect")
            except Exception:
                raise

            result = local(["systemctl", "restart", "udc-resource-broker"], timeout=60)
            if result.returncode != 0:
                raise AcceptanceError(f"broker restart failed: {result.stderr.strip()}")
            wait_until(
                lambda: (
                    local(["systemctl", "is-active", "--quiet", "udc-resource-broker"]).returncode
                    == 0
                ),
                30,
                1,
                "broker restart",
            )
            status = require_ok(
                ssh_cli(self.member_a, ["status", allocation_id]),
                "status after restart",
            )
            if status.get("allocation", {}).get("status") != "active":
                raise AcceptanceError(f"allocation did not survive restart: {status}")
            if not marker.is_file():
                raise AcceptanceError("workspace marker disappeared after restart")

            listed = require_ok(
                ssh_cli(self.member_a, ["job", "list", allocation_id]),
                "job list after broker restart",
            )
            if not any(item.get("name") == job_name for item in listed.get("jobs", [])):
                raise AcceptanceError("job did not survive broker restart")
            logs = require_ok(
                ssh_cli(
                    self.member_a,
                    ["job", "logs", allocation_id, "--name", job_name],
                ),
                "job logs after broker restart",
            )
            if "job-started" not in str(logs.get("logs", "")):
                raise AcceptanceError("job log did not survive broker restart")
            attach = subprocess.Popen(
                [
                    "ssh",
                    "-tt",
                    *SSH_DEFAULT_OPTIONS,
                    self.member_a,
                    shlex.join(
                        [
                            "udc-resource",
                            "job",
                            "attach",
                            allocation_id,
                            "--name",
                            job_name,
                        ]
                    ),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                time.sleep(2)
            finally:
                attach.terminate()
                try:
                    attach.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    attach.kill()
            require_ok(
                ssh_cli(
                    self.member_a,
                    ["job", "stop", allocation_id, "--name", job_name],
                ),
                "job stop",
            )
            wait_until(
                lambda: (
                    not any(
                        item.get("name") == job_name
                        for item in require_ok(
                            ssh_cli(self.member_a, ["job", "list", allocation_id]),
                            "job list after stop",
                        ).get("jobs", [])
                    )
                ),
                10,
                1,
                "job stop",
            )
            expires = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
            connection = sqlite3.connect(self.db_path)
            try:
                connection.execute(
                    "UPDATE allocations SET expires_at = ? WHERE id = ?",
                    (expires, allocation_id),
                )
                connection.commit()
            finally:
                connection.close()

            def expired() -> bool:
                payload = ssh_cli(self.member_a, ["status", allocation_id])
                allocation = payload.get("data", {}).get("allocation", {})
                return allocation.get("status") == "expired"

            wait_until(expired, 90, 5, "test allocation expiry")
            if container_exists(container_id):
                raise AcceptanceError("expired container still exists")
            if not marker.is_file():
                raise AcceptanceError("workspace marker was deleted by expiry")
            bystander_status = require_ok(
                ssh_cli(self.member_b, ["status", bystander_id]),
                "bystander status",
            )
            if bystander_status.get("allocation", {}).get("status") != "active":
                raise AcceptanceError("bystander allocation was affected")
            print(
                "[pass] expiry/restart/disconnect: allocation expired, "
                "workspace kept, bystander intact"
            )
        finally:
            for alias, current_id in list(self.allocations):
                if current_id in {allocation_id, bystander_id}:
                    try:
                        ssh_cli(alias, ["release", current_id], timeout=30)
                    except Exception:
                        pass
                    self.allocations.remove((alias, current_id))

    def run(self) -> None:
        audit = local(
            ["bash", str(Path(__file__).with_name("audit_legacy.sh"))],
            timeout=120,
        )
        if audit.returncode != 0:
            raise AcceptanceError(
                "legacy audit failed; resolve critical findings before active acceptance:\n"
                + audit.stdout
                + audit.stderr
            )
        expected = int(self.config.get("hardware", {}).get("gpu_count", 8))
        for user in (self.member_a_user, self.member_b_user):
            if not user:
                continue
            gpu_check = local(
                [
                    "bash",
                    str(Path(__file__).with_name("verify_gpu_permissions.sh")),
                    "--member",
                    user,
                    "--expected-gpus",
                    str(expected),
                ],
                timeout=180,
            )
            if gpu_check.returncode != 0:
                raise AcceptanceError(
                    f"GPU permission verification failed for {user}:\n"
                    + gpu_check.stdout
                    + gpu_check.stderr
                )
        data = require_ok(ssh_cli(self.member_a, ["list"]), "initial list")
        existing = [
            allocation
            for allocation in data.get("allocations", [])
            if allocation.get("status") in {"pending", "active"}
        ]
        if existing:
            raise AcceptanceError(
                "refusing to run acceptance while other allocations exist: "
                + ", ".join(str(item.get("id")) for item in existing)
            )
        self.test_real_ssh()
        self.test_concurrent_apply()
        self.test_unmanaged_gpu_holder()
        self.test_expiry_restart_disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--member-a", required=True, help="SSH alias for member A")
    parser.add_argument("--member-b", required=True, help="SSH alias for member B")
    parser.add_argument(
        "--member-a-user", help="local Linux user for direct GPU/Docker denial checks"
    )
    parser.add_argument("--member-b-user", help="second local Linux user for direct denial checks")
    parser.add_argument("--gpu", required=True, type=int, help="dedicated test GPU index")
    parser.add_argument("--image", help="test image (default: config image.base)")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("UDC_RESOURCE_CONFIG", "/etc/udc-resource/config.toml")),
    )
    parser.add_argument("--yes", action="store_true", help="required for destructive acceptance")
    parser.add_argument(
        "--allow-test-container",
        action="store_true",
        help="required for the temporary GPU holder container test",
    )
    parser.add_argument(
        "--allow-db-time-travel",
        action="store_true",
        help="required to shorten a test allocation's expires_at in SQLite",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        print("run as root", file=sys.stderr)
        return 1
    if not args.yes:
        print("refusing destructive acceptance without --yes", file=sys.stderr)
        return 1
    if not args.allow_test_container:
        print("--allow-test-container is required", file=sys.stderr)
        return 1
    if not args.allow_db_time_travel:
        print("--allow-db-time-travel is required", file=sys.stderr)
        return 1
    if args.member_a == args.member_b:
        print("member aliases must be different", file=sys.stderr)
        return 1
    try:
        config = load_config(args.config)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"cannot read config: {exc}", file=sys.stderr)
        return 1
    acceptance = Acceptance(args, config)
    try:
        acceptance.run()
    except Exception as exc:
        print(f"[fail] {exc}", file=sys.stderr)
        return 1
    finally:
        acceptance.cleanup()
    print("active acceptance PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
