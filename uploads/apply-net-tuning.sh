#!/bin/sh
# apply-net-tuning.sh
# 写入 BBR + 网络/内核 sysctl 调优参数。
#
# 设计目标：
#   - 单一数据源：import.py（导入时）与 update-templates.py（更新已有模板）共用本脚本
#   - 跨发行版 / 跨内核安全：不直接运行 sysctl -p（离线定制环境内核未必匹配），
#     只落盘配置，首次开机由 systemd-sysctl / openrc 应用；不支持的项会被内核忽略
#   - 幂等：重复执行只会覆盖同名文件，结果一致
#   - 永远 exit 0，避免 virt-customize 因个别项失败而中断
set -u

# 1) 开机自动加载 tcp_bbr 模块（很多新内核已内建，加载失败无害）
mkdir -p /etc/modules-load.d 2>/dev/null || true
echo tcp_bbr > /etc/modules-load.d/bbr.conf 2>/dev/null || true

# 2) 写入 sysctl 调优（不支持的参数内核会在开机时忽略并记录到日志，不影响其他项）
mkdir -p /etc/sysctl.d 2>/dev/null || true
cat > /etc/sysctl.d/99-network-tuning.conf << 'EOF'
# Managed by pve-import-template (apply-net-tuning.sh) —— 请勿手工编辑
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
EOF

# 3) 清理旧版脚本可能留下的重复 ARP/旧调优文件（避免冲突，可选）
# 旧版本写入的就是同名文件 99-network-tuning.conf，已被上面覆盖，无需额外处理。

echo "[net-tuning] /etc/sysctl.d/99-network-tuning.conf written." >&2
exit 0
