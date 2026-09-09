# 通过 Cloudflare Tunnel 从外网访问家庭 NAS 服务（不动防火墙）

> 一套免费、不用开端口、不暴露公网 IP 的方案，把家里 NAS 上的反代 / 管理页 / 任意 Web 服务安全暴露到公网。
> 适用：家庭宽带网关无法关防火墙、IPv6 入站被挡、IPv4 是 CGNAT、或单纯不想暴露源站。

---

## 一、为什么需要它

普通「端口映射 / DDNS」方案要**外网主动打进你家端口**，被网关防火墙拦死。Cloudflare Tunnel 反过来：

```
外网用户
   │  https://svc.yourdomain.com
   ▼
Cloudflare 边缘节点（公网入口，随便进）
   │  ← 已有加密隧道（NAS 主动出站建好，防火墙挡不住「出站」）
   ▼
NAS 上的 cloudflared 进程  ──→  127.0.0.1:你的服务端口
```

- 连接方向是**反向**的：NAS 自己出站连 CF 的 443，隧道建好后 CF 把请求顺着隧道送回 NAS。
- 网关防火墙只拦「入站」，拦不住「出站」，**所以不用关防火墙、不用开端口**。
- 域名只负责好记；CF 全程隐藏你家真实 IP / 端口。

---

## 二、前置条件

- 一个域名（任意注册商都行，本文以 `yourdomain.com` 占位）
- Cloudflare 免费账号（Zero Trust 首次可能要绑卡，预授权 $0 不扣费；**纯银联单标卡常被拒，用 Visa/Mastercard 双币卡**）
- NAS 能跑 Docker（x86_64 / arm64 均可）

---

## 三、步骤

### 步骤 1 · 把域名 NS 迁到 Cloudflare

1. 登录 Cloudflare → **Add a site** → 填 `yourdomain.com` → 选 **Free**。
2. CF 给两个 Nameserver，形如 `xxx.ns.cloudflare.com` / `yyy.ns.cloudflare.com`。
3. 去你的域名注册商控制台 → 把 DNS 服务器改成 CF 这两个（**只改 NS，注册商不变**）。
4. 等 CF 面板变 **Active**（通常 10–60 分钟，最长 48 小时）。

> ⚠️ 迁移前先导出原解析记录备份（可回滚）。迁 NS 后原注册商的解析记录失效，需在 CF 面板重建。
> ⚠️ 确认域名的 **DNSSEC 未开启**（无 DS 记录），否则 NS 切换会被卡。

### 步骤 2 · 建命名隧道，拿 token

1. CF 左侧 **Zero Trust → 网络（Networks）→ 隧道（Tunnels）→ 创建隧道**。
2. 隧道名随便（如 `nas-tunnel`），运行方式选 **Docker**。
3. 复制给出的 **令牌（token）**，一长串 `eyJ...`。

> ❌ **不要用 quick tunnel（`trycloudflare.com`）**：快速隧道不支持 SSE 流式，Gemini / 大模型流式输出会断。必须用**命名隧道 + 自有域名**。

### 步骤 3 · NAS 跑 cloudflared

在 NAS 上（能 SSH 或直接 docker 命令）：

```bash
docker run -d \
  --name cloudflared \
  --network host \
  --restart unless-stopped \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token <你的TUNNEL_TOKEN>
```

**两个关键点：**
- `--network host` **必须带**：否则容器内 `localhost` 不等于宿主机，打不到 `127.0.0.1:你的端口`。
- 别加 `--protocol http2`：用默认 `quic`（见踩坑 1）。

验证：
```bash
docker logs cloudflared 2>&1 | tail -20
# 看到 "INF ... connection registered" / "HEALTHY" 即成功
```

### 步骤 4 · 加 Public Hostname（把域名指向本地服务）

Zero Trust → 网络 → 隧道 → 你的隧道 → **配置 → 公共主机名 → 添加公共主机名**：

| 目标服务 | 子域 | 类型 | URL | 附加设置 |
|---|---|---|---|---|
| 反代 / 普通 HTTP 服务 | `svc` | **HTTP** | `localhost:8045` | 默认 |
| NAS 管理页（HTTPS 自签） | `nas` | **HTTPS** | `localhost:5667` | TLS → 禁用 TLS 证书验证：**开** |

> 类型选 **HTTPS** 时，URL 只填 `localhost:5667`（不要带 `http://` 前缀）。
> 保存后 CF 自动加 CNAME，配置**实时推送给 NAS，不用重启容器**。

### 步骤 5 · 验证

外网（手机流量 / 公司网络）访问：

```bash
curl https://svc.yourdomain.com/
# 返回 200 = 通
```

若服务本身有鉴权（如 API Key），未带 token 应返回 `401`，说明链路通且安全。

---

## 四、客户端怎么填（三环境只换 URL）

| 项 | 外网 | 局域网 | Tailscale |
|---|---|---|---|
| Base URL | `https://svc.yourdomain.com` | `http://192.168.1.100:8045` | `http://100.x.x.x:8045` |
| API Key | 同一个 `<API_KEY>` | 同 | 同 |

- 家里设备优先用**局域网直连**，最快最稳、不耗 CF 带宽。
- 外网才走 `svc.yourdomain.com`（绕 CF 边缘，国内晚高峰 50–300ms）。

---

## 五、实施中必踩的 5 个坑（实测）

### 坑 1 · NAS 跑了 Clash/Mihomo 透明代理，cloudflared 握手失败

**现象**：容器一直 `INF ... connection terminated` / TLS EOF 重连，日志里 edge IP 是 `198.18.x.x`（假 IP）。

**根因**：Mihomo/Clash 用 **TProxy（CONNMARK）** 在网络层劫持 53 端口 DNS，把所有域名解析成 `198.18.x.x` 假 IP。改 `/etc/resolv.conf` **无效**（拦截在网络层）。`--protocol http2` 拨到假 IP 后 TLS 握手失败。

**解法（两层，缺一不可）**：
1. **第一层（握手自愈）**：cloudflared **用默认 `quic` 协议**（别加 `--protocol http2`）。quic 在假 IP 环境下能自愈并连上真 CF 边缘，日志出现 `HEALTHY` + 连到真实节点即正常。
2. **第二层（持久化，必须做）⚠️**：`quic` 只是「握手瞬间自愈」，**不是根治**。一旦 NAS 重启、Mihomo TUN 重新接管，cloudflared 连 `argotunnel.com` 的流量仍会被甩到代理 + 返假 IP，隧道**照样挂**（实测：fnOS 升级重启后外网 530/502）。根治要在 Mihomo `config.yaml` 把隧道域名放行：
   ```yaml
   dns:
     fake-ip-filter:
       - '+.argotunnel.com'        # 让 argotunnel 返回真实 IP（而非 198.18.x.x 假 IP）
   rules:
     - DOMAIN-SUFFIX,argotunnel.com,DIRECT   # 隧道流量直连 Cloudflare，不走代理
     - MATCH,proxy
   ```
   改完 `systemctl restart mihomo`（仅 TUN 实例），再 `docker restart cloudflared`（清旧假 IP 缓存）。**改一次永久生效，重启不再复发。**

### 坑 2 · 子域拼写错误，查错域名一直 NXDOMAIN

**现象**：你以为建的是 `gemini.yourdomain.com`，实际手滑填成 `gmini`，CF 真建在 `gmini` 上且可通；你狂查 `gemini` 当然 NXDOMAIN。

**解法**：CF 面板找到那条路由 → 编辑子域改对 → 保存。配置实时推送，不用重启容器。

### 坑 3 · NAS 管理页是 HTTPS 自签，路由类型填错

**现象**：`400 The plain HTTP request was sent to HTTPS port`。

**解法**：路由**类型选 HTTPS**，URL 填 `localhost:5667`，并在附加设置 → TLS → **禁用 TLS 证书验证：开启**（因为自签证书不被 CF 信任）。

### 坑 4 · 原证书用 `dns_ali` 签发，迁 NS 后续期失败

**现象**：你原来用 acme.sh 的 `dns_ali`（阿里云 API）签发 `yourdomain.com` 证书，NS 迁到 CF 后，续期脚本调阿里云 API 改不了 CF 的解析记录，**证书到期后续期失败，全站 HTTPS 挂**。

**解法**：把 acme.sh 签发方式切到 `dns_cf`：
1. CF 面板 → My Profile → API Tokens → 建一个 **Zone:DNS:Edit** 权限的 token。
2. 拿到 `CF_Token` + `CF_Account_ID`，写入 acme.sh 环境：
   ```bash
   export CF_Token="<你的CF_DNS_TOKEN>"
   export CF_Account_ID="<你的ACCOUNT_ID>"
   acme.sh --issue -d yourdomain.com --dns dns_cf
   ```
3. 在证书到期前完成（建议提前一周）。

### 坑 5 · quick tunnel 不支持 SSE 流式

**现象**：用 `cloudflared tunnel --url http://localhost:8045` 一行命令起的快速隧道，调 Gemini 等流式接口会断流。

**解法**：必须用**命名隧道 + 自有域名**（步骤 2–4），命名隧道才支持 SSE。

### 坑 6 · NAS 重启后隧道/反代不自动起来

**现象**：NAS 升级或重启后，外网 530/502，cloudflared 在重连循环。排查发现反代容器 `restart=no`、或 Mihomo 起来后把 `argotunnel.com` 又劫持了（见坑 1 第二层）。

**解法（重启自愈三保障，缺一不可）**：
1. `argotunnel.com` DIRECT 规则已写进 Mihomo `config.yaml`（坑 1 第二层）——否则每次重启隧道必挂。
2. 关键容器设 `--restart unless-stopped`：
   ```bash
   docker update --restart unless-stopped cloudflared antigravity-manager
   ```
3. Mihomo / Docker 开机自启（systemd `enabled`）：
   ```bash
   systemctl enable mihomo.service docker.service
   ```

**实测重启自愈**：发 `sudo reboot` 后，SSH 约 59s 恢复，cloudflared 容器约 +40s 自动拉起，外网 `gemini`/`nas` 约 +60s 全绿，**整套约 2 分钟自动恢复，无需人工干预**。

---

## 六、安全加固（强烈建议）

公开域名后，陌生人能扫到端点。建议：

1. **Cloudflare Access（最重要，护「人访问」的页面）**
   Zero Trust → **访问控制** → **应用程序** → **添加应用程序** → 类型 **自托管** → 子域 `nas` / 域 `yourdomain.com` → 策略操作 **允许**、选择器 **电子邮件**、值填你自己的邮箱 → 身份验证关闭「使用 Cloudflare One Client」、确认 **一次性 PIN** 已勾 → **添加应用程序**。
   - 效果：别人连登录页都看不到，先被 CF 弹邮箱验证页拦住；验证方式=邮箱收 **Verify 链接**（点一下即过，不用手敲码），同浏览器会话期内不再弹。
   - **NAS 管理页（`nas.yourdomain.com`）必须加**——fnOS 今年爆过认证绕过/路径遍历漏洞，Access 能从外部挡掉未授权访问与未知 0day。
   - ⚠️ **翻车点**：邮箱值**必须带 `@`**（写成 `abc.gmail.com` 会导致策略 100% 阻止）；**别在 Tunnel 路由页勾「用 Access 保护」**（旧集成要 AUD tag，会卡死），Access 必须单独在 Zero Trust 建。

   > 🚨 **关键铁律：API 端点不要套 Access 邮箱 OTP！**
   > 反代（`svc.yourdomain.com`）、网关这类**被程序/客户端调用**的服务，**绝不能**加邮箱 OTP——程序不会收邮件、不会点链接，套上后你的 AI 客户端 / 脚本调 `base_url` 全返回 CF 验证页、直接挂掉。这类端点靠**自身 API Key 鉴权**即可（无 key 返回 401）。Access 只给「人开浏览器」的服务（如 fnOS 管理页）叠。

2. **WAF + 速率限制**
   Security → WAF → 单 IP 每分钟 > 30 请求则挑战/拦截，防爆破和刷量。
3. **强随机 API Key**
   确认你的反代 `API_KEY` 是长随机串，别用弱口令。
4. **只在需要时开公网**
   家里/手机走 Tailscale（不暴露公网）；纯公网场景才用 `svc.yourdomain.com`。

---

## 七、国内延迟实测

| 维度 | 实测（2026，深圳） |
|---|---|
| 带宽 | 免费档 HTTP 不限速 |
| 延迟 | CF 大陆无直连节点，绕 HK/SG：非高峰 50–100ms，晚高峰 100–300ms |
| 文本 / API 调用 | 完全够用 |
| 大文件 / 视频 | 晚高峰跨境被限流到 5–30Mbps，**不适合** |

---

## 八、Cloudflare 顺手能白嫖的福利

| 产品 | 免费额度 | 用途 |
|---|---|---|
| **Tunnel** | 不限 | 本文主角 |
| **Access** | 50 用户 | 护域名，加二次鉴权 |
| **Workers** | 10 万次/天 | 边缘跑 JS、反代、缓存 |
| **Pages** | 无限带宽 | 托管静态站 |
| **R2** | 10GB + **出站免流量费** | 图床 / NAS 备份仓（S3 最贵就是出网费） |
| **WAF / DDoS** | 基础 | 公网暴露后更安心 |
| **DNS-01 验证** | 免费 | 修 acme.sh 续期坑 |

> 注意：R2 / Workers AI 等部分产品开通要绑卡，纯银联单标卡常被拒。**Tunnel / Access / Workers / Pages / DNS / CDN 全不绑卡可用。**

---

## 九、FAQ

**Q：必须租服务器吗？**
A：不用。CF Tunnel 是免费的 `cloudflared` 小进程跑在你 NAS 上，主动出站连 CF 边缘，不买服务器、不绑卡（Tunnel 本身）。

**Q：国内银行卡能用吗？**
A：Tunnel 本身不用卡。若 Zero Trust 弹绑卡，用带 Visa/Mastercard 标识的双币卡（预授权 $0 不扣费）；纯银联单标卡大概率被拒。

**Q：一个隧道能管多个服务吗？**
A：能。同一条隧道加多个 Public Hostname（`svc` / `nas` / `openclaw`...），一个域名管所有 NAS 服务。

**Q：源站 IP 会暴露吗？**
A：默认开启「橙色云」代理后，公网只见 CF IP，你家 IP / 端口全藏。

**Q：全家桶押 CF 有风险吗？**
A：有。一旦 CF outage 全断，建议备好回滚（Tailscale / frp）。

---

## 十、一键 Checklist

- [ ] 域名 NS 已迁 CF，面板 Active
- [ ] DNSSEC 未开
- [ ] 建命名隧道，拿到 token
- [ ] NAS 起 cloudflared（`--network host`，默认 quic）
- [ ] NAS 跑 Mihomo/Clash 透明代理时，**在 config.yaml 把 `argotunnel.com` 设 DIRECT + 真实 IP**（否则重启必挂）
- [ ] 关键容器 `docker update --restart unless-stopped cloudflared <反代容器>`；Mihomo/Docker `systemctl enable`
- [ ] 加 Public Hostname（HTTP/HTTPS + noTLSVerify 按需）
- [ ] 外网实测返回 200 / 401
- [ ]（必做）给「人访问」的管理页叠 Cloudflare Access；**API 端点不要套 Access**
- [ ]（待办）acme.sh 切 `dns_cf`，避免证书续期失败
