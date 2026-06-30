#!/bin/bash
set -e

# 用法：
#   bash run.sh [storage] [start-vmid] [extra import.py args...]
# 例：
#   bash run.sh local-lvm 9000 --only-new
#
# 默认：storage=local-lvm，start-vmid=9000

storage="${1:-local-lvm}"
start_vmid="${2:-9000}"
shift $(( $# > 2 ? 2 : $# )) || true   # 余下参数原样透传给 import.py
extra_args=("$@")

# 嵌套虚拟化（幂等：避免重复写入 modprobe.conf）
modprobe -r kvm_intel 2>/dev/null || true
modprobe kvm_intel nested=1 2>/dev/null || true
if ! grep -qs 'kvm_intel nested=1' /etc/modprobe.d/modprobe.conf 2>/dev/null; then
    echo "options kvm_intel nested=1" >> /etc/modprobe.d/modprobe.conf
fi

apt update -y
apt upgrade -y
apt install -y curl wget gnupg2 git

# 克隆或更新仓库
if [ -d pve-import-template/.git ]; then
    git -C pve-import-template pull --ff-only || true
else
    git clone https://github.com/ISIFNET/pve-import-template.git
fi
cd pve-import-template

bash setup.sh

echo
echo "==> 即将导入模板：storage=${storage} start-vmid=${start_vmid} ${extra_args[*]}"
echo "    （查看可用模板：python3 import.py --list；仅导入新增可加 --only-new）"
echo
./import.py "$storage" "$start_vmid" "${extra_args[@]}"
