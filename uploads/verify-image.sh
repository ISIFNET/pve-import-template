#!/bin/sh
# verify-image.sh —— 构建收尾前核对注入是否真的生效；不达标返回非 0，整个构建失败（旧模板保持在役）。
#
# 旧版注入脚本一律 exit 0：qemu-guest-agent 装不上、root 登录没打开，导入照样报「成功」，
# 平台克隆出来才发现密码重置 / fstrim 不工作。这里补上闸门：
#   1. qemu-guest-agent 二进制存在，且会随系统启动
#   2. sshd 的**生效配置**（sshd -T，考虑 drop-in 与 Match 之后）里 permitrootlogin = yes
#   3. templates.yaml 的 min_packages 安全下限（构建信息里的 MIN_PACKAGE 行）：包已装且版本不低于下限
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

# ---- 4) 安全下限（templates.yaml min_packages → /etc/pve-template-release 的 MIN_PACKAGE=<包> <最低版本>）----
# 镜像站滞后时 apk / apt 升级拿到的还是有漏洞的版本：这里比对实装版本，不够就让构建失败（旧模板保持在役），不靠升级脚本的退出码
BUILD_INFO=/etc/pve-template-release

# 已装版本；没装输出空
installed_version() {
  if command -v apk >/dev/null 2>&1; then
    awk -v p="$1" 'BEGIN{RS=""; FS="\n"} { name=""; ver=""; for (i=1;i<=NF;i++) { if ($i ~ /^P:/) name=substr($i,3); if ($i ~ /^V:/) ver=substr($i,3) } if (name==p) { print ver; exit } }' \
      /lib/apk/db/installed 2>/dev/null
  elif command -v dpkg-query >/dev/null 2>&1; then
    dpkg-query -W -f='${db:Status-Status} ${Version}\n' "$1" 2>/dev/null | awk '$1=="installed"{print $2; exit}'
  elif command -v rpm >/dev/null 2>&1; then
    rpm -q --qf '%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n' "$1" 2>/dev/null | head -n 1 | grep -v 'not installed'
  fi
}

# 0 = 已装版本 >= 下限；1 = 更低；2 = 比不了（没有可用的比较工具）
version_at_least() {
  have="$1"; want="$2"
  if command -v apk >/dev/null 2>&1; then
    case "$(apk version -t "$have" "$want" 2>/dev/null)" in
      '>'|'=') return 0 ;;
      '<') return 1 ;;
      *) return 2 ;;
    esac
  elif command -v dpkg >/dev/null 2>&1; then
    dpkg --compare-versions "$have" ge "$want" && return 0
    dpkg --compare-versions "$have" lt "$want" && return 1
    return 2
  elif command -v rpm >/dev/null 2>&1; then
    case "$want" in *:*) ;; *) want="0:$want" ;; esac
    r="$(rpm --eval "%{lua: print(rpm.vercmp('$have', '$want'))}" 2>/dev/null)"
    case "$r" in
      -1) return 1 ;;
      0|1) return 0 ;;
    esac
    return 2
  fi
  return 2
}

if [ -r "$BUILD_INFO" ]; then
  # 读完再循环：管道里的 while 在子 shell，改不了 fail
  floors="$(sed -n 's/^MIN_PACKAGE=//p' "$BUILD_INFO")"
  OLDIFS=$IFS
  IFS='
'
  for line in $floors; do
    IFS=$OLDIFS
    pkg="${line%% *}"; want="${line#* }"
    have="$(installed_version "$pkg")"
    if [ -z "$have" ]; then
      log "FAIL: $pkg is not installed (template requires >= $want)"
      fail=1
      continue
    fi
    version_at_least "$have" "$want"
    case $? in
      0) log "OK: $pkg $have >= $want" ;;
      1) log "FAIL: $pkg $have is older than the required $want (security floor; the package mirror may be behind)"; fail=1 ;;
      *) log "FAIL: cannot compare $pkg $have with $want (no version comparison tool in the image)"; fail=1 ;;
    esac
  done
  IFS=$OLDIFS
fi

exit "$fail"
