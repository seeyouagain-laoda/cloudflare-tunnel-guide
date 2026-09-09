#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""飞牛 fnOS NAS 全量体检（Cloudflare 公网增强版，脱敏通用版）

适用：跑了 Cloudflare Tunnel 的家庭 NAS，一键体检「磁盘/RAID/系统/服务/暴露面/SSH/备份 + CF 公网可达」。

使用前请修改下方 CONFIG 区（全部占位符改成你自己的）：
  - NAS_HOST   : SSH 登录串，形如 "user@192.168.1.100"
  - SSH_KEY    : 本机私钥路径（key 登录优先；也可改为密码登录）
  - SUDO_PASS  : sudo 密码（脚本用 `echo pass | sudo -S` 提权；若你的 sudo 免密可留空，并改 _ssh_run）
  - DOMAIN     : 你的域名（不含子域），如 "yourdomain.com"
  - SUB_GEMINI / SUB_NAS / SUB_OPENCLAW : 你在 CF 面板建的子域（openclaw 没加公网路由就留空）

依赖：本机有 ssh 客户端（OpenSSH）即可，无需 paramiko。
用法：python nas_healthcheck_cf.py
"""
import sys, io, re, os, subprocess, time, shutil
HERE = os.path.dirname(os.path.abspath(__file__))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ───────── CONFIG（改成你自己的） ─────────
SSH_EXE  = shutil.which("ssh") or r"C:/Windows/System32/OpenSSH/ssh.exe"
SSH_KEY  = os.path.expanduser("~/.ssh/your_nas_key")   # TODO: 改成你的私钥
SSH_HOST = "user@192.168.1.100"                        # TODO: 改成 "用户@NAS内网IP"
SSH_PASS = "CHANGE_ME"                                 # TODO: sudo 密码（若免密 sudo 可留空并改 _ssh_run）
DOMAIN   = "yourdomain.com"                            # TODO: 你的域名
SUB_GEMINI   = "gemini"    # CF 面板建的 gemini 子域（反代，API 端点，不加 Access）
SUB_NAS      = "nas"       # CF 面板建的 nas 子域（管理页，已加 Access 邮箱 OTP）
SUB_OPENCLAW = ""          # 未加公网路由则留空；加了就填 "openclaw"

# 本地反代/网关端口（按你 NAS 实际情况改）
PORT_ANTIGRAVITY = 8045    # Gemini 反代
PORT_OPENCLAW    = 9091    # OpenClaw 网关

# ───────── SSH 封装 ─────────
def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"

def _ssh_run(cmd, sudo, to):
    remote = (f'echo "{SSH_PASS}" | sudo -S bash -c {_q(cmd)}') if sudo else cmd
    ssh_cmd = [
        SSH_EXE, "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=30",
        SSH_HOST, remote,
    ]
    try:
        r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=to + 20)
        return r.stdout or "", r.stderr or "", r.returncode
    except Exception as e:
        return "", f"SSH_ERR: {e}", -1

def sh(cmd, sudo=True, to=120):
    return _ssh_run(cmd, sudo, to)

def local_curl(url, to=20):
    """从本机 curl 公网域名（验证 Cloudflare 边缘可达 = 外网访问等价）。"""
    try:
        r = subprocess.run(
            f'curl -s -o /dev/null -w "%{{http_code}}" --max-time {to} "{url}"',
            shell=True, capture_output=True, text=True, timeout=to + 10)
        return (r.stdout or "").strip() or "EMPTY"
    except Exception as e:
        return f"ERR:{e}"

R = []
def add(cat, title, ok, detail=""):
    R.append((cat, title, ok, detail))
    tag = {True: "PASS", False: "FAIL", None: "INFO"}.get(ok, "INFO")
    print(f"[{tag}] ({cat}) {title}" + (f" — {detail}" if detail else ""))

print("=" * 70)
print(" 飞牛 NAS 全量体检（CF 公网增强版） ", time.strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 70)

# ───────── 1. 磁盘布局 ─────────
o, _, _ = sh("lsblk -d -o NAME,SIZE,ROTA,MODEL 2>/dev/null; echo '---MOUNT---'; df -hT 2>/dev/null | grep -vE 'tmpfs|overlay|fuse'")
add("硬件/磁盘", "磁盘布局与挂载", None, "")
print(o)

# ───────── 2. 全量 SMART ─────────
o, _, _ = sh(r"""
for d in /dev/sd?; do
  [ -e "$d" ] || continue
  echo "===== $d ====="
  smartctl -H "$d" 2>/dev/null | grep -iE 'test result|overall'
  smartctl -A "$d" 2>/dev/null | awk '
    /Reallocated_Sector_Ct/||/Current_Pending_Sector/||/Offline_Uncorrectable/||/Temperature_Celsius|/||/Airflow_Temperature_Celsius|/
    {print $1, "raw="$10}'
done
""", to=180)
add("硬件/磁盘", "全量 SMART 健康", None, "详见下")
print(o)
bad = []
for d in ["sda", "sdb", "sdc", "sdd"]:
    oo, _, _ = sh(f"smartctl -A /dev/{d} 2>/dev/null")
    pend = re.search(r"Current_Pending_Sector.*?(\d+)$", oo, re.M)
    reall = re.search(r"Reallocated_Sector_Ct.*?(\d+)$", oo, re.M)
    unc = re.search(r"Offline_Uncorrectable.*?(\d+)$", oo, re.M)
    temp = re.search(r"(?:Temperature_Celsius|Airflow_Temperature_Celsius).*?(\d+)$", oo, re.M)
    p = int(pend.group(1)) if pend else 0
    r = int(reall.group(1)) if reall else 0
    u = int(unc.group(1)) if unc else 0
    t = int(temp.group(1)) if temp else 0
    if p or r or u:
        bad.append(f"/dev/{d}: 待映射={p} 重映射={r} 离线不可纠={u}")
    if t >= 55:
        bad.append(f"/dev/{d}: 温度偏高 {t}C")
add("硬件/磁盘", "坏扇区/温度危险指标", (not bad), ("; ".join(bad) if bad else "无重映射/待映射/不可纠错误，温度正常"))

# ───────── 3. RAID / md ─────────
o, _, _ = sh("cat /proc/mdstat 2>/dev/null; echo '---DETAIL---'; for m in /dev/md*; do [ -e $m ] && mdadm --detail $m 2>/dev/null | grep -E 'State|Raid|Failed|Active|Working|Spare'; done")
add("硬件/RAID", "存储池/RAID 状态", None, "详见下")
print(o)
if re.search(r"degraded|\[_U\]|\[U_\]|_U_|removed", o, re.I):
    add("硬件/RAID", "阵列降级/失败检测", False, "发现降级或失败标记")
else:
    add("硬件/RAID", "阵列降级/失败检测", True, "未发现 degraded/failed 标记")

# ───────── 4. 系统分区 ─────────
o, _, _ = sh("df -h / 2>/dev/null; echo '---SWAP---'; free -h | grep -i swap")
add("系统/分区", "root 用量", None, "详见下")
print(o)
m = re.search(r"/\s+\S+\s+\S+\s+(\d+)%", o)
if m and int(m.group(1)) >= 85:
    add("系统/分区", "root 用量告警", False, f"root {m.group(1)}%")
elif m and int(m.group(1)) >= 70:
    add("系统/分区", "root 用量关注", None, f"root {m.group(1)}%")
else:
    add("系统/分区", "root 用量", True, f"root {m.group(1) if m else '?'}%")

# ───────── 5. 服务存活 ─────────
o, _, _ = sh(r"""
echo '--- docker ---'; systemctl is-active docker 2>/dev/null
echo '--- mihomo ---'; systemctl is-active mihomo.service 2>/dev/null
echo '--- docker ps ---'; docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null
""")
add("软件/服务", "核心服务存活", None, "详见下")
print(o)
for svc in ["docker", "mihomo"]:
    mm = re.search(rf"--- {svc} ---\s*\n(\S+)", o)
    st = mm.group(1) if mm else "?"
    add("软件/服务", f"{svc} active", st == "active", st)
add("软件/服务", "容器全部 Up", (o.count("Up") >= 1), f"Up 数={o.count('Up')}")

# ───────── 6. 端口暴露面 ─────────
o, _, _ = sh("ss -ltnp 2>/dev/null | grep -vE '127.0.0.1|::1' | head -40")
add("安全/暴露面", "外网可达监听端口(0.0.0.0)", None, "详见下")
print(o)

# ───────── 7. SSH / fnOS 版本 ─────────
o, _, _ = sh(r"""
echo '--- sshd ---'; grep -iE '^PermitRootLogin|^PasswordAuthentication' /etc/ssh/sshd_config 2>/dev/null
echo '--- fnOS ---'; cat /usr/trim/etc/version 2>/dev/null | head -2
""")
add("安全/鉴权", "SSH 配置 + fnOS 版本", None, "详见下")
print(o)
if "PermitRootLogin yes" in o:
    add("安全/鉴权", "root SSH 登录", False, "PermitRootLogin yes 开启")
else:
    add("安全/鉴权", "root SSH 登录", True, "未开放 root 直登或已限制")
m = re.search(r"(\d+\.\d+\.\d+)", o)
ver = m.group(1) if m else "?"
ok_ver = None
if ver != "?":
    try:
        ok_ver = tuple(int(x) for x in ver.split(".")) >= (1, 1, 20)
    except Exception:
        ok_ver = None
add("安全/鉴权", "fnOS 版本(>=1.1.20 安全基线)", ok_ver, f"fnOS {ver}")

# ════════════ 8. Cloudflare 公网健康 ════════════
print("\n" + "#" * 70)
print(" 8. Cloudflare 公网服务健康")
print("#" * 70)

o, _, _ = sh("docker ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null | grep -i cloudflared")
add("CF隧道", "cloudflared 容器存活", "Up" in o, (o.strip() or "未找到 cloudflared 容器"))

o, _, _ = sh(f"""
echo -n 'antigravity='; curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://localhost:{PORT_ANTIGRAVITY}/ 2>/dev/null; echo
echo -n 'openclaw='; curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://localhost:{PORT_OPENCLAW}/ 2>/dev/null; echo
""")
ag = re.search(r"antigravity=(\d+)", o)
oc = re.search(r"openclaw=(\d+)", o)
ag_code = ag.group(1) if ag else "?"
oc_code = oc.group(1) if oc else "?"
ag_ok = True if ag_code == "200" else (None if ag_code in ("?", "000", "") else False)
oc_ok = True if oc_code == "401" else (None if oc_code in ("?", "000", "") else False)
add("CF隧道/服务", f"antigravity {PORT_ANTIGRAVITY} 本地存活(200)", ag_ok, f"HTTP {ag_code}")
add("CF隧道/服务", f"openclaw {PORT_OPENCLAW} 本地存活(401鉴权)", oc_ok, f"HTTP {oc_code}")

print("--- 外网可达性（本机 curl 公网域名）---")
ext_g_root = local_curl(f"https://{SUB_GEMINI}.{DOMAIN}/") if SUB_GEMINI else "SKIP"
ext_g_models = local_curl(f"https://{SUB_GEMINI}.{DOMAIN}/v1/models") if SUB_GEMINI else "SKIP"
ext_nas = local_curl(f"https://{SUB_NAS}.{DOMAIN}/") if SUB_NAS else "SKIP"
ext_oc = local_curl(f"https://{SUB_OPENCLAW}.{DOMAIN}/") if SUB_OPENCLAW else "SKIP(未加公网路由)"
print(f"  {SUB_GEMINI}.{DOMAIN}/         = {ext_g_root}")
print(f"  {SUB_GEMINI}.{DOMAIN}/v1/models = {ext_g_models}")
print(f"  {SUB_NAS}.{DOMAIN}/            = {ext_nas}")
print(f"  {SUB_OPENCLAW}.{DOMAIN}/       = {ext_oc}")
add("CF公网/gemini", "外网 gemini UI 可达(200)", ext_g_root == "200", f"HTTP {ext_g_root}")
add("CF公网/gemini", "外网 gemini models 鉴权(401)", ext_g_models == "401", f"HTTP {ext_g_models}")
add("CF公网/nas", "外网 nas 经 Access 拦截(302/403)", ext_nas in ("302", "403"), f"HTTP {ext_nas}")

# ───────── 汇总 ─────────
print("\n" + "=" * 70)
crit_fail = [r for r in R if r[2] is False]
warn = [r for r in R if r[2] is None]
print(f"危险项(FAIL): {len(crit_fail)} | 关注项(INFO/WARN): {len(warn)}")
for r in crit_fail:
    print(f"  ❌ {r[1]}: {r[3]}")
print("=" * 70)
