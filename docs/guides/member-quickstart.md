# 成员使用指南

本指南覆盖三种入口：没有账号的新成员、已有账号的老成员、以及日常自助使用。

## 0. 安装打包客户端（推荐）

不要从仓库手动复制 Python 脚本。维护者发布带签名的客户端后，从 GitHub Release
下载对应平台的 `citespec-resource-<version>-<platform>-full.zip`：

- Windows x64：从签名清单读取 `installer_sha256`，解压后运行
  `powershell -File install.ps1 -Verify <installer_sha256>`，然后打开新的 PowerShell。
- Linux x64：从签名清单读取 `installer_sha256`，解压后运行
  `bash install.sh --verify <installer_sha256>`。

`--verify`/`-Verify` 是强制参数；缺失或摘要不匹配会在写入任何文件前退出。先用
`verify_release.py` 校验完整包和签名，步骤见 `docs/client-distribution.md`。

启动器会自动检查并安装签名更新；也可以手动运行
`citespec-resource update --check` 或 `citespec-resource update`。
客户端只更新本机 payload，不会修改服务器、Portal 或实验室网络。
完整的构建、签名、镜像和回滚说明见 `docs/client-distribution.md`。

## 1. 新成员：从申请到首次使用

1. **获取邀请**：向管理员（见下方联系方式）索取邀请编号与邀请密钥。
2. **提交申请**（两种方式）：
   - 浏览器打开申请页（地址见实验室公告），填写姓名、期望用户名、归属、用途、
     联系方式，粘贴 **`.pub` 公钥**（只提交公钥，绝不提交私钥）；
   - 或在能访问服务器的机器运行 `citespec-resource join --portal <申请页地址>` 按向导填写。
   - 提交成功后会得到 **申请编号 + 回执密钥**（回执密钥仅显示一次，请保存）。
3. **查看进度**：`citespec-resource join --status --portal <申请页地址>`（输入申请编号
   与回执密钥；回执只通过 POST 提交，不会出现在 URL 或浏览器历史中）。
4. **管理员批准后**：账号、中央登录公钥与工作区会自动开通。申请人看到"已开通"
   即可继续；开户失败时管理员会看到具体阶段并可重试。
5. **配置工作站**（没有服务器账号时也可先准备 SSH 公钥）：

   ```bash
   citespec-resource setup --server <服务器地址或别名> --user <批准通知中的Linux用户名>
   # 无人值守或有管理员公布的指纹时：
   # citespec-resource setup --server <地址> --user <用户名> \
   #   --fingerprint SHA256:<管理员公布值> --yes
   ```

   首次连接会显示服务器 SHA256 主机指纹，必须通过管理员提供的可信渠道核对后
   明确确认；不接受陌生主机，也不关闭主机密钥检查。指纹必须与端点实际提供的
   主机密钥算法一致：有些服务器只提供 RSA 主机密钥，此时管理员公布的应是该
   RSA 密钥的指纹，客户端只比较协商到的那把密钥。
   向导写入独立托管 SSH 片段和 `~/.config/citespec-resource/client.toml`，只包含
   连接信息，不覆盖已有 SSH 配置或私钥，也不上传私钥。
6. **开始工作**：

   ```bash
   citespec-resource apply --gpu-count 1 --cpus 8 --memory 32 --hours 4
   citespec-resource connect <工作环境ID>   # 或 citespec-resource gui
   ```

   `start` 是较新客户端的快捷命令；服务端版本较旧时会返回"不支持的子命令"。
   先用 `citespec-resource --help` 确认服务端实际提供的命令，`apply` 在所有
   版本都可用。正常创建/排队/释放资源是自助的，不需要每次找管理员审批。

**VPN Portal（上线后）**：在校园网或先连接学校 VPN，打开实验室公布的
`https://citespec-portal.<lab-domain>/register` 即可提交注册，不需要邀请码。管理员
批准后会通过可信渠道发来一次性激活码；打开 `/activate` 设置密码、绑定 TOTP，
之后用 `/login` 登录。Portal 只能查看本人成员/服务状态，以及管理员审批；GPU/容器
申请、释放、队列、Shell、VS Code 和文件传输仍使用上面的 CLI/SSH 命令。

联系入口：申请页上的"支持"信息，或实验室公告中的计算资源管理员。

## 2. 老成员：从旧 SSH 到新工作台

已有 Linux 账号不会因为同名自动获得成员资格，也不会继续使用 home
`~/.ssh/authorized_keys`。请先把常用 `.pub` 公钥交给管理员；管理员用
`admin members import-legacy` 审核账号、UID/组、密钥、有效期与配额后，你再运行
`setup`。不要直接要求管理员手工追加 home 公钥。

- 直接 `ssh <服务器别名>`：菜单已中文化并按你的状态展示——
  有工作环境时突出"继续工作"，没有时引导创建。
- 在容器内：受限容器（当前生产配置）没有 root、根文件系统只读，不能安装系统包；
  Python 包用 `pip install --user`，`HOME` 指向 `/workspace`，包落在
  `/workspace/.local` 并随工作区保留。只有管理员明确开放 development profile 时
  才有容器 sudo。`/workspace` 之外的修改会随容器销毁。
- 本地工作台：在你自己的电脑运行 `citespec-resource gui`。
- VS Code：从工作台的"VS Code"按钮进入，或 `citespec-resource vscode <工作环境>`。
- 直接执行 `bash` / `docker` / `nvidia-smi` 等宿主机命令会被拒绝，
  stderr 会给出正确入口。

## 3. 日常自助

| 操作 | 命令 |
|---|---|
| 快捷创建（服务端支持 `start` 时按张数自动选卡） | `citespec-resource start --gpu-count 1` |
| 通用创建（所有版本） | `citespec-resource apply --gpu-count 1 --cpus 8 --memory 32 --hours 4` |
| 使用预设（服务端支持时） | `citespec-resource start --preset standard` |
| 精确指定卡号 | `citespec-resource apply --gpus 3` |
| 查看我的状态与公钥 | `citespec-resource me` |
| 等待队列 | `citespec-resource queue list` / `queue cancel <id>` |
| 训练任务 | `citespec-resource job start <工作环境> --name train -- python train.py` |
| 查看任务持久日志 | `citespec-resource job logs <工作环境> --name train` |
| 取回要保留的产物 | `citespec-resource pull <工作环境> <路径> <本地路径>`（`release` 前后都可以） |
| 结束环境并释放资源 | `citespec-resource release <工作环境>` |
| 公钥轮换 | `citespec-resource me keys add ~/.ssh/new.pub` → `me keys revoke <key-id>` |

注意：结束环境会停止训练并销毁容器临时环境（容器 rootfs、`/tmp` 中的修改）；
`/workspace` 数据保留，包括 `pip install --user` 写入的 `/workspace/.local`，需要
干净环境时自行清理。释放后 `job files`/`job logs` 仍可读取持久日志；`/workspace`
也仍归你所有，因此 `release` 之后仍可用 `pull` 取回 checkpoint、指标等产物（已释放
的环境只允许下载，不再接受上传）。排队不是失败——保留等待或取消均可；重复提交
同一个申请（相同幂等键）
不会重复创建。
添加公钥会立即同步到服务器中央公钥文件；撤销公钥会立即关闭该成员的 Broker
连接，并让旧 SSH ControlMaster 不能继续建立新的受管连接。撤销前确认自己还有
另一个可用公钥，否则需要联系管理员恢复。
