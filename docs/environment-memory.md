# 科研环境记忆与旧容器认领

这套功能回答一个具体问题：**半年后还能不能把当时能跑的训练环境原样搭回来？**
它把“环境”从一句聊天记录变成可审计、不可变的版本，同时明确区分三件常被混为一谈的事：

| 维度 | 含义 | 取值 |
| --- | --- | --- |
| 采集保真度 `capture_fidelity` | 当时读到了多少依赖信息 | `complete` / `partial` / `missing` |
| 构建验证 `verification` | 镜像是否真的构建出来、是否跑过项目 | `unverified` / `built` / `functional_passed` / `functional_failed` |
| 可见性 `visibility` | 谁能看到这个版本 | V1 仅 `private`（项目所有者）；`admin_shared` 供管理员共享 |

采集完整不等于镜像能构建，镜像能构建也不等于项目还能训练。三者分开记录，避免
“看起来完整”被当成“已经验证”。

## 数据模型

```text
科研项目 (research_projects)
  └── 环境档案 (environment_profiles，V1 每个项目一个默认档案)
        └── 环境版本 (environment_versions，不可变)
              ├── manifest（基础镜像、Python 发行包、conda 记录、dpkg 包、锁文件清单）
              ├── 锁文件副本（受大小上限约束）
              └── 构建记录（镜像 tag/id、证据、状态）
```

版本一旦创建就不能修改；新采集产生新版本。所有写入都要求成员当前处于 active 状态，
管理员保留审计与审核路径，但版本仍记录真实的创建成员。

## 成员流程

```bash
# 1. 建项目（自动带一个默认环境档案）
citespec-resource project create --name reconstruction
citespec-resource project list

# 2. 在 active allocation 上采集（只读探针，不运行成员代码）
citespec-resource env capture --project reconstruction --allocation <allocation-id>

# 3. 查看与比较
citespec-resource env list --project reconstruction
citespec-resource env show reconstruction@v1
citespec-resource env diff reconstruction@v1 reconstruction@v2

# 4. 从版本构建镜像（需要管理员打开 environment_builds_enabled）
citespec-resource env build reconstruction@v1
# 构建是同步请求，CLI 默认最多等 3600 秒；可用 --timeout 调整

# 5. 用环境版本启动，而不是手写镜像名
citespec-resource apply --gpus 0 --cpus 8 --memory 32 --hours 12 \
  --env reconstruction@v1

# 6. 训练跑通后，把该租约标记为功能验证通过
citespec-resource env verify reconstruction@v1 --allocation <allocation-id>
```

`--env` 与 `--image` 互斥。队列与租约在请求时固定环境版本、构建 id 和镜像 id；
真正创建容器前会重新校验，镜像被删除或被替换时**直接失败**，不会静默回退到别的镜像。

验证状态只能向前推进：`unverified → built → functional_passed`，失败可以重试。
`env verify` 要求该 allocation 确实是用这个环境版本启动的，避免把别的容器的成功
算到这个版本头上。

## 旧容器认领

早于本功能存在的容器不会被自动“升级”为新版本，也不会用 `docker commit` /
`docker export` 冒充可复现构建。流程是：

```bash
# 管理员：扫描主机上非本系统管理的容器，只登记脱敏元数据
citespec-resource admin legacy-scan
citespec-resource admin legacy-sources

# 成员：查看可认领来源（不显示容器名、完整 id、labels），提交申请
citespec-resource legacy list
citespec-resource legacy claim src_xxx --project reconstruction --reason "2025 年的训练环境"
citespec-resource legacy claims

# 管理员：审核
citespec-resource admin legacy-claims --status pending
citespec-resource admin legacy-decide clm_xxx --approve --note "已核对"
# 或 --reject --note "不属于该项目"

# 管理员：批准后做只读采集，生成 origin=legacy_claim 的版本（通常 partial）
citespec-resource admin legacy-capture clm_xxx
```

规则：

- 同一来源同时只允许一个已批准认领；存在其他待处理申请时，批准返回冲突，管理员
  必须显式驳回或让申请人撤回，系统不自动替人做选择。
- 一个成员对同一来源只能有一条待处理/已批准申请。
- 已停止的容器不会被启动，也不会 exec；只从归档读取固定路径
  `/etc/os-release` 与 `/opt/conda/conda-meta`。
- 运行中的容器使用与普通采集相同的固定探针，但以 `65534:65534`（nobody）执行。
- 归档读取有字节与条目上限，拒绝绝对路径、`..` 与 tar-slip。
- V1 不做 root exec；探针失败时回退到固定路径归档读取，并在版本 notes 中写明。

## 构建边界

- 两阶段构建：CPU-only 构建容器以成员 UID 在 broker 目录内创建 venv，然后生成
  最小 Dockerfile（`FROM` 固定基础镜像 ID + `COPY` venv），`docker build --network none`
  且没有 `RUN` 步骤。
- 基础镜像必须仍能解析出与采集时一致的 digest，否则停止构建。
- **V1 不支持自动 apt 安装系统包**。缺失的系统依赖会记录为未满足需求；管理员需要
  提供一个已包含这些包的基础镜像。
- **conda 包只记录，不自动重建 conda 环境**。`python.packages` 是解释器
  site-packages 中实际存在的发行包，构建时用 pip 安装；`python.conda_packages`
  保留 conda-meta 的原始清单，供人工核对。`_libgcc_mutex`、`bzip2` 这类 conda-only
  包不能 pip 安装，必须由基础镜像提供。
- 构建缓存按版本保留（默认每版本 2 个），超过 `env_build_cache_gb` 时清理旧镜像。

## 管理员开关

全部默认关闭，部署本身不改变现有行为：

```toml
[policy]
environment_enabled = false       # 总开关
environment_builds_enabled = false # 依赖总开关；构建与功能验证
legacy_claims_enabled = false      # 依赖总开关；旧容器扫描/认领
env_state_dir = "/var/lib/citespec-resource/envs"
env_build_cpus = 4.0
env_build_memory_gb = 8
env_build_timeout_seconds = 1800
env_build_cache_gb = 50
env_build_keep_per_version = 2
env_max_versions_per_project = 50
```

开启顺序：`environment_enabled` → 采集可用；再按需开启构建或旧容器认领。关闭任一
上层开关会立即拒绝对应命令，但不会删除已保存的版本。

## GUI

本地 GUI 的“环境”标签页提供：项目创建/归档、版本列表与 manifest、版本对比、
构建、功能验证、旧容器可认领列表与认领申请。管理员标签页提供旧容器扫描、来源列表、
批准/驳回与只读采集。GUI 只是 CLI 的本地代理，权限与门控仍由服务端执行。

## 已知限制

- 采集是只读快照，不保证逐字节还原；`partial` 版本需要人工补全。
- conda 环境的构建走 pip，不会复现 conda 的二进制包；conda-only 包需要基础镜像
  已包含，`python.conda_packages` 只用于核对。
- 锁文件采集有单文件/总量上限，超限会被跳过并在 manifest 中反映。
- 旧容器停止后其镜像层可能仍可读，但无法证明曾经如何启动；因此 legacy 版本默认
  不是“已构建”，仍需走正常构建与功能验证。
