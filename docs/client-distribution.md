# 客户端分发与自动更新

本文面向维护者和成员，说明打包客户端的安装、更新、发布和回滚。Portal 仍只允许
VPN 内访问；客户端更新不改变 Portal 的启用状态。

## 1. 设计

客户端由两部分组成：

- `citespec-resource`：稳定的 PyInstaller 启动器（Windows 为
  `citespec-resource.exe`），负责检查签名更新并启动当前版本。
- `versions/<version>/citespec-resource-app`：真正的 CLI/GUI payload。

启动器最多每 24 小时检查一次更新。更新清单和 payload 都使用 HTTPS；清单使用
Ed25519 签名，payload 使用 SHA-256 校验。新版本先解压到临时目录，确认包含
payload 后再原子替换 `current.json`，旧版本保留用于回滚。

首次安装没有已安装的启动器可用，因此签名清单还包含 `first_install` 段：每个平台
的 full zip、zip 内 `install.sh`/`install.ps1` 的 SHA-256 和大小。用户先运行
`verify_release.py` 校验清单签名与 full zip，再用清单中的 `installer_sha256`
通过 `--verify`/`-Verify` 认证安装脚本。

本仓库固定嵌入更新公钥：

```
LcxxIJY0DcCdDVzMcuk5pIJ4jaTz2hSJ6I50n2E9MvU=
```

签名私钥只保存在维护者的本地忽略文件 `lab/update-signing.local.json`，并应存入
GitHub Actions secret `CITESPEC_UPDATE_SIGNING_KEY`（base64 的 32 字节原始种子）。
不要把私钥、PEM 文件或带私钥的清单提交到仓库。

首个签名版本为
[`v0.1.0`](https://github.com/Mr-enthalpy/infrastructure/releases/tag/v0.1.0)，
包含 Windows/Linux x64 payload、full zip 和签名清单。

## 2. Windows x64 首次安装

1. 从 GitHub Release 下载
   `citespec-resource-<version>-windows-x64-full.zip`、
   `citespec-resource-manifest.json` 和 `verify_release.py`/`verify_release.ps1`。
2. 先校验完整包（需要 Python 3.11+ 与 `cryptography`）：

   ```powershell
   python verify_release.py `
     --manifest citespec-resource-manifest.json `
     --package citespec-resource-<version>-windows-x64-full.zip `
     --platform windows-x64
   ```

   输出中的 `installer_sha256` 是清单签名的安装脚本摘要。
3. 解压后运行 `install.ps1 -Verify <installer_sha256>`。默认安装到
   `%LOCALAPPDATA%\CitespecResource`，并把该目录加入当前用户的 `PATH`。
4. 打开新的 PowerShell，运行：

   ```powershell
   citespec-resource setup --server <服务器地址> --user <Linux 用户名>
   citespec-resource start --gpu-count 1
   citespec-resource gui
   ```

`setup` 只写入工作站连接片段和
`%USERPROFILE%\.config\citespec-resource\client.toml`；不会上传私钥，也不会修改
服务器。

## 3. Linux x64 首次安装

从 Release 下载 full zip 和 `citespec-resource-manifest.json`（也可用
`verify_release.sh` 代替 `python3 verify_release.py`）：

```bash
unzip citespec-resource-<version>-linux-x64-full.zip -d citespec-resource
cd citespec-resource
python3 verify_release.py \
  --manifest ../citespec-resource-manifest.json \
  --package ../citespec-resource-<version>-linux-x64-full.zip \
  --platform linux-x64
bash install.sh --verify <installer_sha256>
```

`installer_sha256` 取自上一步输出（也就是签名清单的
`first_install.linux-x64.installer_sha256`）。脚本默认安装到
`~/.local/opt/citespec-resource`，并在 `~/.local/bin` 建立
`citespec-resource` 软链接。确认 `~/.local/bin` 在 `PATH` 中后运行 `setup`。

首次安装的残余信任缺口：校验器本身、清单和 zip 都从同一 HTTPS 源下载。若下载
通道被完全控制，攻击者可以同时替换三者。发布页与应用商店之外应通过独立渠道公布
`verify_release.py` 的 SHA-256（或公钥指纹），首次安装时人工比对；安装完成后，
后续更新由启动器内置的签名公钥保护，不再依赖该初始通道。

## 4. 更新、检查与回滚

启动器会自动更新。手动操作：

```bash
citespec-resource update --check
citespec-resource update
```

可用环境变量：

| 变量 | 用途 |
|---|---|
| `CITESPEC_UPDATE_MANIFEST` | 覆盖更新清单 URL（例如内部镜像） |
| `CITESPEC_SKIP_UPDATE=1` | 本次不检查更新 |
| `CITESPEC_UPDATE_TIMEOUT` | 启动器清单请求超时秒数（默认 5；手动 `update` 固定 10） |
| `CITESPEC_UPDATE_DOWNLOAD_TIMEOUT` | 启动器 payload 下载超时秒数（默认 120；手动 `update` 固定 60） |
| `CITESPEC_UPDATE_ALLOW_HTTP=1` | 仅用于本地/CI 测试的 HTTP 或 `file://` 清单；生产不要设置 |
| `CITESPEC_RESOURCE_APP_ROOT` | 覆盖应用根目录（本地测试） |

回滚：关闭客户端，把 `current.json` 的 `version` 改回 `versions/` 中保留的旧版本，
或删除 `current.json` 后重新下载 full zip。启动器只执行 `versions/` 下存在的
payload，不会从任意路径执行文件。

## 5. 构建与发布

预览构建（Windows 或 Linux 上分别执行）：

```bash
python -m pip install -e ".[gui,portal]" pyinstaller
python packaging/build_client.py --platform windows-x64 --output dist
python packaging/build_client.py --platform linux-x64 --output dist
```

输出：

- `citespec-resource-<version>-<platform>.zip`：更新 payload。
- `citespec-resource-<version>-<platform>-full.zip`：首次安装包。
- 签名清单中的 `first_install.<platform>`：full zip 与安装脚本的 SHA-256。

发布 job 还会把独立的 `verify_release.py`、`verify_release.sh` 和
`verify_release.ps1` 作为 Release 资产上传，并用签名清单自检两个平台的 full zip；
缺少 full zip 或 zip 内缺少安装脚本时发布会失败。

版本必须同时与 `pyproject.toml`、`src/citespec_resource/__init__.py` 和
`uv.lock` 中的项目版本一致。发布前运行：

```bash
python packaging/check_version.py --tag v<version>
```

发布流程 `.github/workflows/release.yml` 在 `v*` tag 上构建两个平台的 artifact，
下载后生成并签名 `citespec-resource-manifest.json`，再上传到 Release。若
`CITESPEC_UPDATE_SIGNING_KEY` 未配置或与嵌入公钥不匹配，发布会失败，不会生成
无法验证的清单。

本地检查清单（不用于生产）可用：

```bash
python packaging/make_manifest.py \
  --artifacts-dir dist \
  --version <version> \
  --output dist/citespec-resource-manifest.json \
  --unsigned
```

生产签名应始终由 CI secret 完成，避免私钥出现在命令历史或构建日志中。

## 6. 验收

```bash
python -m pytest -q
python -m ruff check src packaging tests
python -m mypy src
```

2026-10-27 本地结果（Windows 10/11，Python 3.13）：

- 完整测试：`475 passed, 21 skipped`；Ruff check/format、mypy、`node --check`
  和 Bash 语法检查通过。
- Portal 本地 loopback 验收：`tests/test_portal.py tests/test_backup.py` 共
  `25 passed`；使用临时数据库和 fake broker，未启动对外 vhost、未修改 DNS/
  VPN/防火墙、未启用生产 Portal。
- Windows x64 payload 和 full zip 构建成功；启动器 `version --json` 返回
  `protocol=1`、`min_client_protocol=1`。
- 本地签名清单自动更新 `0.1.0 -> 0.1.1` 成功；篡改清单和不可达更新源均被拒绝
  且回退到现有 payload。
- Linux x64 构建由 `.github/workflows/release.yml` 在 Ubuntu runner 上完成；
  `client-build` 运行 `40465970936` 已在 Linux runner 执行 `install.sh` 并确认
  软链接启动器和 payload 的 `version --json` 可用。本地 Windows 主机不能替代
  Linux 运行验收；成员首次 Linux 实际安装/更新仍应在受控 Linux 主机记录。
- 2026-10-27 发布验收：release 运行 `40485285111` 成功，发布
  `citespec-resource-0.1.0-windows-x64.zip`、`...-windows-x64-full.zip`、
  `...-linux-x64.zip`、`...-linux-x64-full.zip` 和
  `citespec-resource-manifest.json`。已重新下载全部资产，验证 manifest Ed25519
  签名、两个 payload 的 SHA-256 和大小；在发布的 Windows full zip 上确认
  `version --json` 返回 `version=0.1.0`、`protocol=1`、
  `min_client_protocol=1`，默认 GitHub 更新源返回 `already up to date`。

Windows 手工验收至少覆盖：

1. 首次运行 full zip 启动器，确认自动下载并启动 payload。
2. 发布带新版本号的 manifest 后，确认启动器更新且 `current.json` 指向新版本。
3. 篡改 manifest 或 payload 后，确认客户端拒绝更新并继续使用旧版本。
4. 断网启动，确认旧版本仍可用；删除当前 payload 时给出明确错误。
5. 运行 `citespec-resource version --json`，确认 `version`、`protocol`、
   `min_client_protocol` 字段存在。
