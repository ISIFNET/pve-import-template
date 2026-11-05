#!/bin/sh
# verify-image.sh —— 构建收尾前核对注入是否真的生效；不达标返回非 0，整个构建失败（旧模板保持在役）。
#
# 旧版注入脚本一律 exit 0：qemu-guest-agent 装不上、root 登录没打开，导入照样报「成功」，
# 平台克隆出来才发现密码重置 / fstrim 不工作。这里补上闸门：
#   1. qemu-guest-agent 二进制存在，且会随系统启动
#   2. sshd 的**生效配置**（sshd -T，考虑 drop-in 与 Match 之后）里 permitrootlogin = yes
set -u

fail=0
log() { echo "[verify] $*"; }

# ---- 1) qemu-guest-agent ----
QGA=""
for p in /usr/bin/qemu-ga /usr/sbin/qemu-ga /usr/libexec/qemu-ga; do
  [ -x "$p" ] && QGA="$p" && break
done
if [ -z "$QGA" ]; then
  log "FAIL: qemu-guest-agent is not installed"
  fail=1
else
  enabled=0
  for l in /etc/systemd/system/*.wants/qemu-guest-agent.service /etc/runlevels/default/qemu-guest-agent; do
    [ -e "$l" ] && enabled=1
  done
  # 部分发行版由 udev 规则 / 静态 WantedBy 拉起（virtio 通道出现时自动启动），也算可用
  for r in /usr/lib/udev/rules.d/*qemu-guest-agent*.rules /lib/udev/rules.d/*qemu-guest-agent*.rules; do
    [ -e "$r" ] && enabled=1
  done
  if [ "$enabled" -eq 1 ]; then
    log "OK: qemu-guest-agent installed ($QGA) and enabled"
  else
    log "FAIL: qemu-guest-agent installed but not enabled at boot"
    fail=1
  fi
fi

# ---- 2) cloud-init（克隆靠它注入密码 / 公钥 / 网络；dist-upgrade 的 autoremove 理论上可能把它带走）----
if command -v cloud-init >/dev/null 2>&1 || [ -x /usr/bin/cloud-init ]; then
  log "OK: cloud-init present"
else
  log "FAIL: cloud-init is not installed"
  fail=1
fi

# ---- 3) sshd 生效配置 ----
SSHD=""
for p in /usr/sbin/sshd /usr/bin/sshd; do
  [ -x "$p" ] && SSHD="$p" && break
done
if [ -z "$SSHD" ]; then
  log "FAIL: openssh server is not installed"
  fail=1
else
  KEYDIR="$(mktemp -d /tmp/pit-verify.XXXXXX)"
  # Debian / Ubuntu 的 sshd 要求特权分离目录存在才肯 -t；libguestfs 里 /run 是空的 tmpfs，临时建、用完删
  PRIVSEP_CREATED=0
  if [ ! -d /run/sshd ]; then
    mkdir -p /run/sshd && chmod 0755 /run/sshd && PRIVSEP_CREATED=1
  fi
  if ssh-keygen -q -t ed25519 -N '' -f "$KEYDIR/k" >/dev/null 2>&1; then
    if ! "$SSHD" -t -o HostKey="$KEYDIR/k" 2>"$KEYDIR/err"; then
      log "FAIL: sshd configuration does not parse: $(head -n 3 "$KEYDIR/err" | tr '\n' ' ')"
      fail=1
    else
      eff="$("$SSHD" -T -C user=root,host=localhost,addr=127.0.0.1 -o HostKey="$KEYDIR/k" 2>/dev/null \
        | awk 'tolower($1)=="permitrootlogin"{print tolower($2); exit}')"
      if [ "$eff" = "yes" ]; then
        log "OK: sshd permitrootlogin=yes"
      else
        log "FAIL: sshd effective permitrootlogin is '${eff:-unknown}', expected yes"
        fail=1
      fi
    fi
  else
    log "WARN: ssh-keygen unavailable inside the image; skipped sshd checks"
  fi
  rm -rf "$KEYDIR"
  [ "$PRIVSEP_CREATED" = 1 ] && rmdir /run/sshd 2>/dev/null
fi

exit "$fail"
