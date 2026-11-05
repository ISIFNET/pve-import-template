#!/bin/sh
# finalize-image.sh —— 模板封装前的清理（等价于 virt-sysprep 里与云镜像相关的那几项），必须最后执行。
#
# 为什么需要：构建时做了包升级，openssh-server / systemd 的 postinst 可能已经生成了 SSH host key、
# machine-id、随机种子；不清掉就会被固化进模板，**所有克隆共用同一把 host key**（中间人风险）、
# 同一个 machine-id（DHCP / journald / 监控 agent 认成同一台机）。
# 克隆首次开机时 cloud-init（cc_ssh）/ systemd 会重新生成这些。
set -u

log() { echo "[finalize] $*"; }

# 1) SSH host keys（首次开机由 cloud-init / sshd-keygen / openrc sshd 重新生成）
rm -f /etc/ssh/ssh_host_* 2>/dev/null
if ls /etc/ssh/ssh_host_* >/dev/null 2>&1; then
  log "FAIL: could not remove SSH host keys"
  exit 1
fi
log "removed SSH host keys"

# 2) machine-id：清空而不是删除（文件不存在时部分 systemd 版本首启会以只读 /etc 失败）
if [ -f /etc/machine-id ]; then
  : > /etc/machine-id
  log "truncated /etc/machine-id"
fi
if [ -f /var/lib/dbus/machine-id ] && [ ! -L /var/lib/dbus/machine-id ]; then
  rm -f /var/lib/dbus/machine-id
fi

# 3) cloud-init 运行状态（等价 cloud-init clean --logs；不删 seed / 配置）
rm -rf /var/lib/cloud/instances /var/lib/cloud/instance /var/lib/cloud/data /var/lib/cloud/sem \
       /var/lib/cloud/handlers 2>/dev/null
rm -f /var/log/cloud-init.log /var/log/cloud-init-output.log 2>/dev/null

# 4) 随机种子
rm -f /var/lib/systemd/random-seed /var/lib/random-seed /var/lib/urandom/random-seed 2>/dev/null
rm -rf /var/lib/seedrng/* 2>/dev/null

# 5) pacman keyring（upgrade-packages.sh 临时生成的那份；首次开机由 pacman-init 重新生成）
if [ -f /etc/pacman.d/.pit-remove-gnupg ]; then
  rm -rf /etc/pacman.d/gnupg /etc/pacman.d/.pit-remove-gnupg
  log "removed the build-time pacman keyring"
fi

# 6) 包管理器缓存 / 索引（缩小模板体积；克隆里第一次 install 前会自己 update）
if command -v apt-get >/dev/null 2>&1; then
  apt-get clean >/dev/null 2>&1 || true
  rm -rf /var/lib/apt/lists/* 2>/dev/null
fi
command -v dnf >/dev/null 2>&1 && dnf clean all >/dev/null 2>&1
command -v yum >/dev/null 2>&1 && yum clean all >/dev/null 2>&1
command -v zypper >/dev/null 2>&1 && zypper --non-interactive clean -a >/dev/null 2>&1
rm -rf /var/cache/apk/* /var/cache/pacman/pkg/* 2>/dev/null

# 7) 日志：轮转出来的旧文件删掉，在用的清空（保留文件本身与权限）
if [ -d /var/log ]; then
  find /var/log -type f \( -name '*.gz' -o -name '*.xz' -o -name '*.old' -o -name '*.[0-9]' \) \
    -exec rm -f {} \; 2>/dev/null
  find /var/log -type f 2>/dev/null | while IFS= read -r f; do : > "$f"; done
  rm -rf /var/log/journal/* 2>/dev/null
fi

# 8) shell 历史、临时目录
rm -f /root/.bash_history /root/.ash_history /root/.history /root/.lesshst /root/.viminfo 2>/dev/null
for h in /home/*; do
  [ -d "$h" ] && rm -f "$h/.bash_history" "$h/.ash_history" 2>/dev/null
done
rm -rf /tmp/* /tmp/.[!.]* /var/tmp/* /var/tmp/.[!.]* 2>/dev/null

log "done"
exit 0
