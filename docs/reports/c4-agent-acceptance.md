# C-4 real research-agent end-to-end acceptance report

Date: 2026-10-28. Host: test instance with 4 x RTX 3090 24 GB, restricted
container profile, active mode. Report prepared by the administrator from the
agent's own report and independent server-side checks. It contains no host
addresses, usernames, keys, or administrator credentials.

## 1. Scope

One ordinary member identity, created by the administrator before the test
(ordinary Linux account, no host sudo, no Docker group, no GPU device group,
50 GiB workspace quota), used the packaged client and
`docs/guides/member-quickstart.md` only. The agent did not receive the
acceptance procedure, pass criteria, broker internals, or administrator
instructions.

Task: one real, scaled-down training run of `reconstruction` at commit
`69bd221`, using the project's own A6 configuration on prepared 512 x 512,
21-band train/validation data. The frozen test set was not uploaded or loaded.

## 2. Execution summary

| Item | Result |
|---|---|
| First training step | ~26 min after reading the guide (about 15 min of that was mechanical) |
| Leases | 2, both released; same GPU returned by normal auto-selection |
| GPU time | 5,614.8 s (~93.6 min), 0.23 s/step |
| Progress | 24,800 steps / 248 epochs, stopped by configured `patience=40`, not by time or step budget |
| Disconnect test | Local client disconnected 3 min; training continued (epoch 14 to 24) |
| Persistence | `best.pt` and `last.pt` hashes identical before release and after reconnection; resume from `last.pt` continued the curve |
| Release | Both allocations reclaimed; all four GPUs back to free; no leftover processes |
| Privilege boundary | Container ran as the member UID, no host Docker/rootfs access, only the allocated GPU visible |

## 3. Training result

`best.pt` (epoch 208), mean over 9 validation scenes:

| Metric | Value |
|---|---|
| PSNR | 30.725 dB |
| SSIM | 0.940 |
| SAM | 9.90 deg |
| Cube relative L2 | 0.249 |
| High-frequency relative L2 | 0.762 |

High-frequency relative L2 improved from 0.942 at 4,000 steps to 0.762 at
24,800 steps (PSNR +0.95 dB, SAM -1.1 deg). The earlier short-budget
"high-frequency failure" diagnosis was therefore mostly a budget effect.

Administrator check: `best.pt`, `last.pt`, `history.json`, `validation.json`,
`execution.json`, and all four prepared-data files match the hashes recorded by
the agent; no `test.npz` exists in the workspace.

## 4. Pass criteria

| Dimension | Verdict | Note |
|---|---|---|
| Identity isolation | Pass | Single ordinary member identity for the whole run |
| Resource admission | Pass after unblock | First attempt rejected because the broker was in monitor mode; after the administrator switched to active, normal admission worked |
| Environment setup | Pass with friction | Read-only root, no container sudo; `pip install --user` into the persistent workspace was the only viable path |
| Real training | Pass | Real parameter updates, logs, checkpoints, metrics |
| Disconnect recovery | Pass | Detached job survived a 3 min client disconnect |
| Persistence | Pass | Results retained across two leases; environment removals matched the documented behavior except `pip --user` persistence |
| Release | Pass | Resources reclaimed, workspace kept for review |
| Privilege boundary | Pass | No host administrative access obtained |

Conclusion: **C-4 core pass**, with blocking onboarding friction and six
non-core issues recorded below. The service should not be offered to more
members until items C4-1 and C4-2 are fixed.

## 5. Resource-management findings

| ID | Severity | Finding | Recommended fix |
|---|---|---|---|
| C4-1 | Blocking | The published host fingerprint did not match the algorithm the endpoint offers (the endpoint only advertises an RSA host key). `setup` failed with a generic "fingerprint mismatch" and no hint about key type. | Publish the fingerprint for the algorithm actually offered; make `setup` print the observed fingerprint, key type, and a next step on mismatch |
| C4-2 | Blocking on Windows | `setup` succeeded in writing the profile, then crashed with `UnicodeDecodeError: 'gbk' codec ...` followed by `AttributeError: 'NoneType' object has no attribute 'strip'`; exit code non-zero. | Decode SSH output as UTF-8 with `errors="replace"`; never call `.strip()` on an optional probe result; return 0 when the profile is valid |
| C4-3 | High | `nvidia-smi` inside the container is broken: the host installs a wrapper that execs `/usr/bin/nvidia-smi.real`, but the NVIDIA runtime injects only the wrapper, so every GPU container gets `nvidia-smi.real: not found`. | Do not rename the host binary; ship a wrapper that resolves the real binary, or bind-mount both files into containers and test in CI |
| C4-4 | High | Client/server version skew: the packaged client documents `start`, but the deployed server CLI has no `start` subcommand and returns "unsupported subcommand". | Deploy matching versions, or have `start` fall back to `apply` when the server does not advertise it |
| C4-5 | Medium | `monitor_only` rejection has no next step ("broker is in monitor-only mode"). Members cannot tell whether to wait or to ask an administrator. | Include "an administrator must run `admin mode active`" in the error detail shown to members |
| C4-6 | Medium | `pull` is refused once the allocation is released (`conflict: allocation is released, not active`), but the guide does not say to retrieve artifacts before `release`. `job files`/`job logs` still work. | Document release-before-pull; consider allowing read-only `pull` of workspace files for released allocations |
| C4-7 | Low | After `release`, `list` still reports `remaining_seconds`, which looks like the allocation is still running. | Omit remaining time for non-active statuses |
| C4-8 | Low | Chinese output is emitted in the Windows console encoding, so agents must transcode before parsing; JSON payloads are unaffected. | Emit UTF-8 and document `chcp 65001`, or keep JSON as the automation path |

Observed but not a defect: `HOME` is `/workspace` in the restricted profile, so
`pip install --user` persists in `/workspace/.local` across leases. This is
consistent with workspace persistence but was not documented.

### 5.1 Remediation status (2026-10-28)

All eight items below are fixed on `main` and the server was upgraded to the
same 0.1.1 package as the client. Rows marked "verified" were re-checked on the
test instance; the remaining confirmation comes from the acceptance rerun.

| Item | Status |
|---|---|
| C4-1, C4-2 | Fixed (#4). `setup` probes the key types the endpoint offers, reports the observed fingerprint, key type, and next step on mismatch, and returns 0 when the profile is valid. Verified against the endpoint: RSA fingerprint announced, no decode crash. |
| C4-3 | Fixed (#5). The broker mounts the wrapper's target read-only into GPU containers. Verified on a real GPU container: `nvidia-smi -L` exited 0 and named the allocated GPU; the container was removed afterwards. |
| C4-4 | Fixed (#5). When the server does not advertise `start`, the client falls back to `apply`. Server and packaged client now run 0.1.1. |
| C4-5 | Fixed (#6). `monitor`/`maintenance` rejections state that an administrator must run `admin mode active` (and allow deployment). Verified live as a member while the broker was in `monitor`. |
| C4-6 | Fixed (#6). The owner or an administrator can `pull` files from a released allocation; uploads and every other state still require `active`. Verified live: a released allocation returned its metrics file, and `file put` returned `conflict: allocation is released, not active`. |
| C4-7 | Fixed (#6). `list`/`status` only render remaining time for active allocations. |
| C4-8 | Fixed (#6). The CLI configures its output streams at startup: redirected output becomes UTF-8, an interactive console keeps its native encoding, both with replacement instead of an exception. |

The upgrade also required three installer fixes found during the maintenance
window: the stale `uv.lock` that omitted the `portal` extra (#7), the portal
sudo check that treated `sudo -l`'s exit status as a privilege grant (#8), and
the staged-venv rename that left dead shebangs in the console scripts (#9).

### 5.2 Acceptance rerun on 0.1.1 (2026-10-28)

The same member identity reran the acceptance with the signed 0.1.1 client
installed from the CI-built candidate package, against the server upgraded to
the same 0.1.1 tree. The task, configuration, and run layout were chosen by the
agent; the administrator only reset the time window (`mode=active`, four free
GPUs, file transfer enabled for the duration) and did not intervene afterwards.
The agent reported zero requests for external assistance (two in the first
round) and completed the work in one lease: 2 GPU / 16 CPU / 48 GiB / 4 h,
applied through the normal path and released 2 h 06 min later.

| Item | Result |
|---|---|
| Real training | Pass. Two matching arms (chromatic / achromatic PSF) trained in parallel on two GPUs inside one lease; 248 and 300 epochs, best checkpoints saved, full validation metrics written |
| Reproducibility | Pass. One arm reproduced an earlier single-GPU run epoch by epoch (max absolute difference 0.0), so single vs. dual GPU and one vs. two concurrent processes do not change the numbers |
| Disconnect recovery | Pass. The client was absent for 6 min mid-run; training continued (epoch 2 to 12) with complete logs |
| Release | Pass. Both allocations released, four GPUs back to `free`, no leftover containers |
| Post-release pull | Pass. Retrieved a file from the released workspace, hash identical to the copy taken before release |
| Administrator check | Checkpoint, history, validation, and execution hashes on the server matched the copies the agent pulled; workspace (2.0 GB) retained; no test-set data in the workspace |
| Privilege boundary | Pass. Member UID, read-only root, no sudo, only the allocated GPUs visible |

Per-item recheck of the first round's findings:

| Item | Rerun result |
|---|---|
| C4-1 | Fixed. A deliberate wrong-fingerprint run printed the offered algorithm, the real RSA fingerprint, and why an ED25519 fingerprint can never match; the correct run then exited 0 |
| C4-2 | Fixed. No decode crash, no traceback, exit code 0, version reported correctly. Residual: on a console whose code page cannot represent full-width punctuation, those characters still render as `?` (see C4-8) |
| C4-3 | Fixed. `start --gpu-count 1` printed the fallback notice and allocated through `apply` |
| C4-4 | Improved. The old `monitor_only` path is gone; an over-sized request now returns `gpu_busy` with the requested and free counts. It still does not suggest queuing, see C4-4 follow-up below |
| C4-5 | Fixed. `nvidia-smi --query-gpu=...` inside the container returned both allocated 3090s |
| C4-6 | Fixed. Post-release `pull` exited 0 with a matching hash |
| C4-7 | Fixed in the rendered output; the JSON body of a released allocation still carries `remaining_seconds` |
| C4-8 | Partially fixed. Redirected and piped output is UTF-8 and JSON parses, but non-JSON output on an interactive console still uses the console encoding and loses unrepresentable punctuation |

New findings from the rerun (non-blocking, not yet fixed):

| ID | Severity | Finding | Recommended fix |
|---|---|---|---|
| C4-9 | Medium | `job start --name <existing name>` does not conflict when only a stale log remains from an earlier lease: it appends to that log, so `job logs` replays the previous session and looks like the new job produced nothing | Refuse to start when the log already exists (or rotate it and say so), instead of appending silently |
| C4-10 | Medium | The container reports the host CPU count (72) although the lease grants 16, and default thread pools follow it; two concurrent trainings over-subscribed the host and ran about 3x slower until the agent set `OMP/MKL/OPENBLAS_NUM_THREADS=4` by hand | Export thread limits matching the allocation, and state in the guide that `nproc` is host-wide |
| C4-11 | Low | `id -un` fails with "cannot find name for user ID 1003" because the container has no passwd entry for the member UID; harmless but confusing in scripts | Add a passwd entry for the member UID at container creation |
| C4-4 follow-up | Low | `gpu_busy` does not mention that removing `--no-queue` would queue instead | Add the next step to the error detail |

Verdict: **C-4 passes on 0.1.1**. All blocking items from the first round are
closed and the member-visible workflow held up without administrator help.
C4-9 and C4-10 are cheap fixes and should land before the service is offered to
more members; C4-7 (JSON field), C4-11, and the C4-4 hint can go in the same
batch. The agent chose to keep the workspace and both checkpoints for review;
they are not deleted.

## 6. Project-side findings

1. The best checkpoint can land on an epoch that `eval_every` does not sample,
   so the best row in `history.json` has no metrics; recompute from
   `validation.json`.
2. `patience=40` with a declared 300-epoch budget stops at 248 epochs, so "use
   the full budget" is impossible without changing the config.
3. Training loads the whole 640 MB npz at start and re-hashes 768 MB; `mmap`
   exists only in uncommitted work.
4. SAM is noisy (it rose at epoch 120 before falling again); train and
   validation loss magnitudes are not comparable because of noise injection.

## 7. Operational findings

- A graphical login manager on the GPU host holds about 15 MiB per GPU. The
  broker treats more than 8 MiB of used memory without a compute process as
  `unmanaged`, so all four GPUs were rejected as unmanaged while the host was
  in `graphical.target`. The host default target is now `multi-user.target`.
- After deployment or a host restart, confirm `admin mode active` before
  announcing the service; a broker left in monitor mode rejects every member
  application with C4-5.

## 8. Evidence retained

- Agent report, timeline, command log, and redacted errors are kept in the
  research project's output directory (outside this repository).
- The workspace (`runs/`, `data/`, `code/`, `.local/`, about 1.2 GB of a
  50 GiB quota) is kept until the administrator finishes review.
- Server-side hashes, `validation.json`, and `execution.json` were re-checked
  by the administrator; the frozen test set was never uploaded.
