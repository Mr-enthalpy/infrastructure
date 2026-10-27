# Citespec 校内/VPN HTTPS 接入需求

> 提交对象：服务器/网络管理
> 版本：2026-10-27
> 范围：DNS、VPN、防火墙、TLS 证书、主机 nginx 入口
> 不包含：Portal 应用内部实现、GPU/容器/训练任务调整

## 1. 目标与边界

目标：

- Citespec 的成员/管理员 Web 入口只允许**校园网或学校 VPN**访问。
- 不开公网 Web 入口，不把管理面暴露到互联网。
- 校内外使用同一个 HTTPS 域名；校外先连校园 VPN，再打开页面。
- 手机和非开发机可以访问：新成员注册、成员信息/状态、只读服务状态、管理员简单审批/成员管理。
- Web 不提供 Web shell、VS Code、文件传输、GPU/容器资源申请或释放；这些继续走 CLI/SSH。
- 应用侧 `[portal].enabled` 默认 `false`；只有本文件第 6 节网络/证书验收通过后
  才启动 Portal 服务。

不做：

- 不把公网 443 映射到本机。
- 不使用/修改现有公网 Apache 入口作为本应用的入口。
- 不开放 8765、8770、broker、管理 GUI、Docker、GPU 到非 loopback 地址。
- 不改动现有 GPU 容器、训练任务、用户数据、备份；不停止/删除无关容器。
- 不修改现有 SSH 入口 `<ssh-port>`；SSH 管理面另行评估。

## 2. 当前状态

- 主机私网地址：`<host-private-ip>`
- 公网地址：`<public-ip>`
- 当前公网 443 由学校 Apache 前端终止，返回 `302` 到 `/ng`，不是本机 nginx。
- 本机 nginx 当前监听 `0.0.0.0:80/443`，使用自签 IP 证书，代理到 `127.0.0.1:8765`。
- 当前 intake 监听 `127.0.0.1:8765`；计划新增 portal 监听 `127.0.0.1:8770`。
- 当前 `apply_url` 仍是 `https://<public-ip>/`，需要改为内网 HTTPS 域名。
- 当前公网 80 超时；不建议依赖 HTTP-01。

## 3. 目标拓扑

```text
手机 / 非开发机（校园网或先连校园 VPN）
  │ HTTPS 443
  ▼
主机 nginx :443（私网监听，仅校园/VPN 网段）
  ├─ citespec-portal.<lab-domain> → 127.0.0.1:8770
  └─ citespec-apply.<lab-domain>  → 127.0.0.1:8765
```

- 公网不发布 A/AAAA 记录。
- 公网 443 不映射到 `<host-private-ip>`。
- 外部 Apache 不参与本方案；如果已有公网 443 到本机的映射，请移除或拒绝。

## 4. 需要服务器/网络管理协助的事项

### 4.1 内网 DNS

建议域名（最终以学校可分配的域名为准）：

- `citespec-portal.<lab-domain>`
- `citespec-apply.<lab-domain>`

需要：

- 两条内网 `A` 记录指向 `<host-private-ip>`。
- 不发布公网 `A`/`AAAA` 记录。
- TTL 建议先设为 `300`，稳定后再调大。
- 如果校园网客户端使用公网 DNS，需要 split-horizon，让校内解析到 `<host-private-ip>`。
- 如果使用公共 CA 的 DNS-01 验证，需要能在公网 DNS 添加 `_acme-challenge` TXT 记录；不需要公网 A 记录。
- 如使用公共 CA，建议配置 CAA，允许对应 CA。

### 4.2 校园 VPN

需要确认/配置：

- 校园 VPN 是网络层 VPN，能把 `<host-private-ip>`（或 `<private-subnet>`）路由给客户端。
- VPN 下发内网 DNS，使客户端能解析 `citespec-portal.<lab-domain>` 和 `citespec-apply.<lab-domain>`。
- 提供校园网和 VPN 客户端源地址段，用于主机 443 防火墙白名单。
- 如果只有 WebVPN/URL 重写型入口，请先说明；该类型可能破坏 API、CSRF、会话和后续状态轮询，需要先做兼容性验证。
- 手机端验收：连接校园 VPN 后能解析内网域名、打开 HTTPS、证书无警告、登录成功。

### 4.3 防火墙

建议规则：

- 入站 443：仅允许校园网和校园 VPN 源地址段；拒绝公网。
- 入站 80：不开放公网；本机如需要可仅做本地跳转，不用于 ACME。
- 不开放 8765、8770 到非 loopback 地址。
- 不新增公网 443 NAT/端口映射。
- IPv6：不发布公网 AAAA；如启用 IPv6，防火墙也必须限制，避免绕过 nginx。
- 出站：允许 DNS、NTP、443（系统更新/证书续期）。
- 现有 SSH `<ssh-port>` 保持不变，不在本需求范围内。

如果 nginx 必须监听 `0.0.0.0:443`，请用防火墙保证只有校园/VPN 网段可达；更建议绑定 `<host-private-ip>:443`。

### 4.4 TLS 证书

优先级：

1. **公共 CA + DNS-01**（推荐）：使用公网域名，但不发布公网 A 记录；通过 `_acme-challenge` TXT 完成签发。手机浏览器无需安装根证书。
2. **学校通配证书**：如果学校有 `*.<university-domain>` 或实验室域名的通配证书，直接使用并确认续期方式。
3. **内部 CA**：可用于纯内网，但成员手机需要安装根证书；不推荐作为首选。

需要明确：

- 证书覆盖 `citespec-portal.<lab-domain>` 和 `citespec-apply.<lab-domain>`。
- 证书链完整；私钥权限 `0600`，仅 root/nginx 可读。
- 自动续期和续期后 reload nginx 的机制。
- 到期前告警（建议 30/14/7 天）。
- 验证稳定后再开启 HSTS；TLS 仅允许 1.2/1.3。
- 不使用 IP 证书；用户不再通过 IP 访问。

### 4.5 主机 nginx

如果 nginx 由 Citespec 侧维护，服务器管理只需完成 DNS/VPN/防火墙/证书；如果由服务器管理维护，请按以下意图配置：

- 443 私网监听，仅校园/VPN 可达。
- 两个独立 vhost：
  - `citespec-portal.<lab-domain>` → `http://127.0.0.1:8770`
  - `citespec-apply.<lab-domain>` → `http://127.0.0.1:8765`
- 未知 Host 返回 `444`/关闭连接，避免 Host 头攻击。
- 保留 `Host`；设置 `X-Forwarded-Proto: https`。
- 由 nginx 覆盖写入 `X-Citespec-Client-IP $remote_addr`，应用只信任来自 loopback 的该头；用于限流和审计。
- 关闭代理缓存；请求体限制 64 KiB（与 Portal/intake 应用上限一致）；设置合理
  connect/read timeout。
- 不配置 `/ws/`、`/shell`、`/vscode`、`/file` 等 Web 终端/文件传输路由。
- 访问日志记录真实客户端 IP；不记录请求体、密码、TOTP、申请回执等敏感内容。
- 安全头：HSTS（验证后）、`X-Content-Type-Options`、`X-Frame-Options`/`frame-ancestors`；CSP 可由应用层统一设置，避免重复冲突。

## 5. 需要服务器管理确认的问题

请尽量逐项答复；不确定的可以写“需确认”。

| # | 问题 | 需要的答复 |
|---|---|---|
| 1 | 可分配的域名/区域是什么？ | 例如 `citespec-portal.<lab-domain>`、`citespec-apply.<lab-domain>` 是否可用 |
| 2 | 内网 DNS 由谁管理？能否添加 A/TXT/CAA？ | 管理方、TTL、是否需要工单 |
| 3 | 是否支持 split-horizon？ | 校内解析到 `<host-private-ip>`，公网无 A |
| 4 | 校园 VPN 是网络层 VPN 还是 WebVPN？ | 类型；是否支持内网 DNS 和到 `<host-private-ip>` 的路由 |
| 5 | VPN/校园网源地址段是什么？ | 用于 443 防火墙白名单 |
| 6 | 是否启用 IPv6？ | 是否发布 AAAA；防火墙是否同样限制 |
| 7 | 证书用哪种方式？ | 公共 CA DNS-01 / 学校通配 / 内部 CA |
| 8 | 如果公共 CA：谁能添加 `_acme-challenge` TXT？ | 是否有 DNS API 或人工流程；续期谁负责 |
| 9 | 如果内部 CA：手机根证书如何分发？ | 是否可接受安装根证书；否则不建议 |
| 10 | 当前公网 443 是否已有到本机的 NAT/映射？ | 如有，请移除/拒绝；本方案不需要 |
| 11 | 主机 443 是否可由防火墙限制到校园/VPN？ | 源地址段、规则管理方 |
| 12 | 主机 nginx 由谁维护？ | Citespec 侧 / 服务器管理；是否可新增 vhost |
| 13 | 是否有维护窗口/变更审批要求？ | 时间、联系人、回滚方式 |
| 14 | 是否有日志留存/安全审计要求？ | 保留周期、是否需要接入学校日志平台 |

## 6. 验收清单

网络/证书侧：

- [ ] 校园网可解析 `citespec-portal` / `citespec-apply`，HTTPS 证书链有效。
- [ ] 手机连接校园 VPN 后同样可解析、打开、证书无警告。
- [ ] 不连 VPN、从公网访问两个域名均超时/不可达。
- [ ] 公网没有对应 A/AAAA 记录；没有公网 443 到本机的映射。
- [ ] `openssl s_client -servername ...` 显示的证书与域名匹配。
- [ ] 证书续期 dry-run 成功；续期后可 reload nginx。
- [ ] 8765、8770 从非 loopback 不可达。
- [ ] IPv6 没有绕过防火墙/nginx 的公网入口。
- [ ] 访问日志能看到真实客户端 IP（校园/VPN 地址），不是 `127.0.0.1`。

应用侧（Citespec 负责）：

- [ ] Portal 登录 + TOTP 正常；管理员和成员权限隔离。
- [ ] 手机可提交新成员注册；管理员可审批。
- [ ] 成员只能看自己的信息；管理员可看申请/成员并做审批/暂停/恢复/撤销。
- [ ] Web 不存在 shell、VS Code、文件传输、资源申请/释放入口。
- [ ] CSRF、会话过期、登录限速、越权访问测试通过。
- [ ] 一次性激活码只显示一次、默认 24 小时过期；`pending_activation` 账号不能
      因成员 `resume` 直接变为 `active`。
- [ ] 管理员角色变更只能走
      `citespec-resource admin portal set-role`；Web 审批/暂停/恢复/撤销要求密码 +
      TOTP 再认证，且不能降权最后一个管理员。
- [ ] 审计记录包含真实客户端 IP、操作者、目标和结果，且不含密码/TOTP/激活码。
- [ ] Portal 账号/会话/激活码/审计数据库纳入备份与恢复演练。

应用侧可在网络变更前先完成本地预验收（不启动对外 vhost、不修改 DNS/防火墙）：

```bash
.venv/bin/python -m pytest tests/test_portal.py tests/test_broker.py \
  tests/test_backup.py tests/test_deploy_scripts.py -q
```

2026-10-27 本地预验收结果：`tests/test_portal.py tests/test_backup.py` 共
`25 passed`；使用临时数据库和 fake broker，未启动对外 vhost，未修改
DNS/VPN/防火墙。完整测试为 `475 passed, 21 skipped`。

本地预验收至少覆盖：登录/TOTP、错误密码限速、CSRF、注册提交、审批生成一次性
激活码、激活设置密码/TOTP、成员只能看本人、成员不能访问管理员路由、管理员
暂停/恢复、CLI 创建首个管理员与角色变更、最后管理员保护、`pending_activation`
不被 `resume` 激活、只读状态、禁止路由返回
404、`portal.db` 备份/恢复。网络侧验收通过后再执行 nginx 配置和首次对外访问。

## 7. 变更与回滚约束

- 变更前备份 nginx 配置、证书、防火墙规则、DNS 记录。
- DNS TTL 先设为 300，便于回滚。
- 保留旧 nginx 配置和证书，直到新入口稳定。
- 回滚方式：恢复 DNS/防火墙/nginx 配置，不删除现有数据。
- 全程不停止、不删除现有 GPU 容器/训练任务，不改动无关数据。
- 不在同一维护窗口内混入 GPU、SSH、root 会话或应用试运行变更。

## 8. 分工建议

| 事项 | 负责方 |
|---|---|
| 内网 DNS、split-horizon、TXT/CAA | 服务器/网络管理 |
| 校园 VPN 路由、DNS、源地址段 | 服务器/网络管理 |
| 防火墙、公网 443 不映射 | 服务器/网络管理 |
| TLS 证书签发/续期/告警 | 服务器/网络管理（或明确移交 Citespec） |
| 主机 nginx vhost | Citespec 侧优先；服务器管理可协助 |
| Portal 应用、账号/TOTP、RBAC、审计 | Citespec 侧 |
| 最终校内外/手机验收 | 双方 |

## 附录 A：nginx 配置意图（供参考，不是最终配置文件）

```nginx
# portal
server {
    listen <host-private-ip>:443 ssl;
    http2 on;
    server_name citespec-portal.<lab-domain>;

    ssl_certificate     /etc/nginx/tls/citespec-fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/citespec.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 64k;

    location / {
        proxy_pass http://127.0.0.1:8770;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        # nginx overwrites this; app trusts it only from loopback
        proxy_set_header X-Citespec-Client-IP $remote_addr;
        proxy_read_timeout 30s;
        proxy_send_timeout 30s;
        proxy_buffering off;
    }
}

# apply / intake: 同样结构，proxy_pass http://127.0.0.1:8765;

# 未知 Host
server {
    listen <host-private-ip>:443 ssl default_server;
    server_name _;
    ssl_certificate     /etc/nginx/tls/citespec-fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/citespec.key;
    return 444;
}
```

说明：

- 如果 nginx 前面还有一层受控代理，才需要 `set_real_ip_from`；当前方案没有公网反代，不需要信任外部 `X-Forwarded-For`。
- 不建议在 nginx 层开放 WebSocket；当前 Web 范围不需要。
- 具体证书路径、日志路径、限流阈值由 Citespec 侧最终配置确定。

## 附录 B：微信沟通话术（可直接复制）

> 老师您好，Citespec 想改成只走校园网/校园 VPN 的 HTTPS 访问，不开公网入口。需要您协助确认内网 DNS、VPN 路由/DNS、443 防火墙和证书方式。具体需求和待确认项我整理在一页文档里，方便时请您看一下；主要想先确认 DNS、VPN 和证书这三项是否可行。谢谢！

## 附录 C：现场实施与回滚顺序

> 仅在批准的维护窗口执行。任何一步失败先回滚该步，不要叠加变更；不要在本窗口内
> 停止/删除现有 GPU 容器、训练任务或无关数据。

### C.1 变更前快照

```bash
sudo nginx -T > /root/citespec-nginx-before.txt
sudo cp -a /etc/nginx /root/citespec-nginx-before
sudo nft list ruleset > /root/citespec-firewall-before.txt  # 或 iptables-save
systemctl status citespec-resource-broker citespec-resource-intake --no-pager
ss -ltnp | grep -E ':(443|8765|8770)\b' || true
docker ps -a --format '{{.Names}} {{.Status}}' > /root/citespec-containers-before.txt
```

同时由网络管理导出 DNS 记录，记录证书序列号/指纹和续期方式。备份只读保存，
不改动现有备份。

### C.2 DNS、VPN、防火墙

1. 添加 `citespec-portal.<lab-domain>` 和 `citespec-apply.<lab-domain>` 的内网
   A 记录，指向 `<host-private-ip>`；不发布公网 A/AAAA。
2. 确认校园网和校园 VPN 客户端能解析两个域名；TTL 先设 300。
3. 443 只允许校园网/VPN 源地址段；不新增公网 443 NAT；8765/8770 不对外。
4. 从校外普通网络确认两个域名不可达；从 VPN 确认可达。

### C.3 TLS 证书

优先使用学校通配证书或公共 CA DNS-01；不要用自签 IP 证书作为成员入口。若由
学校 CA 签发，CSR 的 SAN 必须同时包含两个域名：

```bash
openssl req -new -newkey rsa:3072 -nodes \
  -keyout citespec-portal.key -out citespec-portal.csr \
  -subj "/CN=citespec-portal.<lab-domain>" \
  -addext "subjectAltName=DNS:citespec-portal.<lab-domain>,DNS:citespec-apply.<lab-domain>"
chmod 0600 citespec-portal.key
```

安装 fullchain 后验证：

```bash
openssl verify -CAfile /etc/ssl/certs/ca-certificates.crt \
  /etc/ssl/citespec/citespec-portal.fullchain.pem
```

证书私钥 `0600`，仅 root/nginx 可读；确认续期后 reload nginx 的机制和到期告警。

### C.4 nginx vhost

1. 以 `deploy/nginx/citespec-portal.conf.example` 和 apply 示例为基础，替换
   `<host-private-ip>`、域名、证书路径。
2. 私网地址监听 443；未知 Host 返回 `444`；不配置 `/ws/`、`/shell`、
   `/vscode`、`/file` 等 Web 终端/文件路由。
3. `proxy_set_header X-Citespec-Client-IP $remote_addr;` 必须由 nginx 覆盖写入，
   不信任客户端自带值。
4. 先 `sudo nginx -t`，通过后 `sudo systemctl reload nginx`；失败立即恢复旧配置。

### C.5 Portal 安装与启用

先用 `[portal].enabled = false` 安装；`deploy/install_server.sh` 不会自动启动 Portal。
网络/证书验收通过后，才在维护窗口内：

```bash
# 将 [portal].enabled 改为 true，确认 db_path 在 /var/lib/citespec-resource-portal 下
sudo systemctl enable --now citespec-resource-portal
sudo systemctl status citespec-resource-portal --no-pager
curl -fsS http://127.0.0.1:8770/healthz
```

首个管理员只通过 CLI 创建/改角色，不把 `citespec-portal` 加入
`citespec-admins`；管理员密码/TOTP 不写入文档、日志或命令行历史。

### C.6 现场验收

```bash
# 从校园网或 VPN 客户端执行
curl -fsS --resolve citespec-portal.<lab-domain>:443:<host-private-ip> \
  https://citespec-portal.<lab-domain>/healthz
openssl s_client -connect <host-private-ip>:443 \
  -servername citespec-portal.<lab-domain> </dev/null 2>/dev/null | openssl x509 -noout -subject -issuer -dates
ss -ltnp | grep -E ':(443|8765|8770)\b'
```

必须确认：证书链无警告、手机可登录/TOTP、注册与审批可用、成员只能看本人、
管理员操作需密码+TOTP、审计含真实客户端 IP、无危险 Web 路由、8765/8770 从
非 loopback 不可达、校外普通网络超时。

### C.7 回滚

```bash
sudo systemctl stop citespec-resource-portal
sudo cp -a /root/citespec-nginx-before/. /etc/nginx/
sudo nginx -t && sudo systemctl reload nginx
# 恢复防火墙/DNS 到 C.1 记录；保留 portal.db、备份和容器数据
```

回滚只撤销网络/nginx/Portal 服务变更，不删除数据库、备份、GPU 容器或成员数据。
记录失败步骤、时间、操作者和恢复结果。
