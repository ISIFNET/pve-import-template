#!/bin/sh
# apply-net-tuning.sh
# 写入 BBR + 网络/内核 sysctl 调优参数，并按「不同 init 系统」分别处理模块加载与 sysctl 生效。
#
# 设计目标：
#   - 单一数据源：import.py（导入时）与 update-templates.py（更新已有模板）共用本脚本
#   - 跨发行版 / 跨内核安全：只落盘配置，首次开机由 systemd-sysctl / openrc 应用；
#     不支持的项会被内核忽略（不影响其它项）
#   - 针对 init 系统分别处理：systemd 用 /etc/modules-load.d；openrc/Alpine 用 /etc/modules
#   - 幂等：重复执行只覆盖同名文件 / 去重追加，结果一致
#   - 永远 exit 0，避免 virt-customize 因个别项失败而中断
#
# 说明：virt-customize 运行在 libguestfs 沙箱内，`uname -r` 是沙箱内核而非目标系统内核，
#       因此这里不做「按运行内核」的判断；改为「落盘 + 内核自动忽略不支持项」的稳健策略。
set -u

CONF=/etc/sysctl.d/99-network-tuning.conf

# 读取目标系统信息（/etc/os-release 是目标系统的，可在定制时安全读取）
ID=""; ID_LIKE=""
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release 2>/dev/null || true
fi

# ---------- 1) 模块加载（按 init 系统分别处理） ----------
# 说明：设置 net.ipv4.tcp_congestion_control=bbr 时，内核通常会按需自动加载 tcp_bbr；
#       这里额外确保「开机加载」，双保险。
if [ -d /etc/systemd ] || command -v systemctl >/dev/null 2>&1; then
  # systemd 系（Debian/Ubuntu/RHEL/openSUSE/Arch ...）
  mkdir -p /etc/modules-load.d 2>/dev/null || true
  echo tcp_bbr > /etc/modules-load.d/bbr.conf 2>/dev/null || true
else
  # 非 systemd（Alpine/openrc 等）：使用 /etc/modules，并尽量启用 sysctl 服务
  if [ -f /etc/modules ]; then
    grep -q '^tcp_bbr$' /etc/modules 2>/dev/null || echo tcp_bbr >> /etc/modules
  else
    echo tcp_bbr > /etc/modules 2>/dev/null || true
  fi
  if command -v rc-update >/dev/null 2>&1; then
    rc-update add sysctl boot 2>/dev/null || true
  fi
fi

# ---------- 2) sysctl 调优 ----------
mkdir -p /etc/sysctl.d 2>/dev/null || true
cat > "$CONF" << 'EOF'
# Managed by pve-import-template (apply-net-tuning.sh) —— 请勿手工编辑

# === 核心：BBR 拥塞控制 + 大缓冲（高带宽时延积场景）===
net.core.default_qdisc = fq
net.core.rmem_max = 67108848
net.core.wmem_max = 67108848
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_rmem = 16384 16777216 536870912
net.ipv4.tcp_wmem = 16384 16777216 536870912
net.ipv4.tcp_adv_win_scale = -2
net.ipv4.tcp_sack = 1
net.ipv4.tcp_timestamps = 1
kernel.panic = -1
vm.swappiness = 0

# === 附加：与 BBR/大缓冲互补的稳健增强项（不支持的内核会自动忽略）===
# 长连接空闲后不回退拥塞窗口，避免突发流量重新慢启动
net.ipv4.tcp_slow_start_after_idle = 0
# 缓解路径 MTU 黑洞（部分隧道/云网络常见）
net.ipv4.tcp_mtu_probing = 1
# 启用 TCP Fast Open（客户端 + 服务端）
net.ipv4.tcp_fastopen = 3
# 配合 BBR 限制本地未发送队列，降低 bufferbloat 与延迟
net.ipv4.tcp_notsent_lowat = 131072
# 高 pps 时的网卡入队缓冲
net.core.netdev_max_backlog = 16384
# TIME_WAIT 上限，防止连接表在高并发短连接下爆掉
net.ipv4.tcp_max_tw_buckets = 262144
EOF

echo "[net-tuning] wrote $CONF (id=${ID:-unknown})" >&2
exit 0
