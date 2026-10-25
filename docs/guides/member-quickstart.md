# 成员使用指南

本指南覆盖三种入口：没有账号的新成员、已有账号的老成员、以及日常自助使用。

## 1. 新成员：从申请到首次使用

1. **获取邀请**：向管理员（见下方联系方式）索取邀请编号与邀请密钥。
2. **提交申请**（两种方式）：
   - 浏览器打开申请页（地址见实验室公告），填写姓名、期望用户名、归属、用途、
     联系方式，粘贴 **`.pub` 公钥**（只提交公钥，绝不提交私钥）；
   - 或在能访问服务器的机器运行 `udc-resource join --portal <申请页地址>` 按向导填写。
   - 提交成功后会得到 **申请编号 + 回执密钥**（回执密钥仅显示一次，请保存）。
3. **查看进度**：`udc-resource join --status`（输入申请编号与回执密钥）。
4. **管理员批准后**：账号、登录公钥与工作区会自动开通。申请人看到"已开通"即可继续。
5. **配置工作站**：`udc-resource setup --host <服务器地址>`（写入托管 SSH 片段并验证
   连接；不覆盖你已有的 SSH 配置，不会读取/上传私钥）。
6. **开始工作**：`udc-resource start --gpu-count 1` 创建工作环境，
   `udc-resource connect` 进入终端，或 `udc-resource gui` 打开本地工作台。

联系入口：申请页上的"支持"信息，或实验室公告中的计算资源管理员。

## 2. 老成员：从旧 SSH 到新工作台

- 直接 `ssh <服务器别名>`：菜单已中文化并按你的状态展示——
  有工作环境时突出"继续工作"，没有时引导创建。
- 在容器内：可以 `sudo`、安装系统包、运行训练；`/workspace` 数据持久保留。
- 本地工作台：在你自己的电脑运行 `udc-resource gui`。
- VS Code：从工作台的"VS Code"按钮进入，或 `udc-resource vscode <工作环境>`。
- 直接执行 `bash` / `docker` / `nvidia-smi` 等宿主机命令会被拒绝，
  stderr 会给出正确入口。

## 3. 日常自助

| 操作 | 命令 |
|---|---|
| 快捷创建（按张数，服务端自动选卡） | `udc-resource start --gpu-count 1` |
| 使用预设 | `udc-resource start --preset standard` |
| 精确指定卡号 | `udc-resource apply --gpus 3` |
| 查看我的状态与公钥 | `udc-resource me` |
| 等待队列 | `udc-resource queue list` / `queue cancel <id>` |
| 训练任务 | `udc-resource job start <工作环境> --name train -- python train.py` |
| 查看任务持久日志 | `udc-resource job logs <工作环境> --name train` |
| 结束环境并释放资源 | `udc-resource release <工作环境>` |
| 公钥轮换 | `udc-resource me keys add ~/.ssh/new.pub` → `me keys revoke <key-id>` |

注意：结束环境会停止训练、删除临时安装环境与 HOME；`/workspace` 数据保留。
排队不是失败——保留等待或取消均可；重复提交同一个申请（相同幂等键）不会重复创建。
