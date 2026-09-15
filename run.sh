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

# 嵌套虚拟化（按 CPU 厂商选择模块；幂等：避免重复写入 modprobe.conf）
if grep -qi 'vendor_id.*AuthenticAMD' /proc/cpuinfo; then
    kvm_mod=kvm_amd
else
    kvm_mod=kvm_intel
fi
modprobe -r "$kvm_mod" 2>/dev/null || true
modprobe "$kvm_mod" nested=1 2>/dev/null || true
if ! grep -qs "^options ${kvm_mod} nested=1" /etc/modprobe.d/*.conf 2>/dev/null; then
    echo "options ${kvm_mod} nested=1" >> /etc/modprobe.d/kvm-nested.conf
fi

# 只保证 git 可用；apt 源修复、依赖安装与升级统一交给 setup.sh
# （PVE 9 未订阅时 enterprise 源会让 apt update 返回非 0，这里容忍失败）
if ! command -v git >/dev/null 2>&1; then
    apt-get update || true
    apt-get install -y git
fi

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
