#!/bin/sh
# install-qga.sh —— 安装并启用 qemu-guest-agent
# 适配 Debian/Ubuntu（含 EOL，自动切 archive 源）、RHEL/CentOS/Alma/Rocky、openSUSE、Arch。
# 目标：即使装不到也不会让 virt-customize 失败；最后必须 exit 0。
set -u

ok=0

fix_debian_eol_sources() {
  # 切到 archive.debian.org，并关闭 Valid-Until 校验
  mkdir -p /etc/apt/apt.conf.d || true
  printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99ignore-valid-until || true
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    sed -i -E 's@https?://deb\.debian\.org/debian@http://archive.debian.org/debian@g' "$f" || true
    sed -i -E 's@https?://security\.debian\.org/debian-security@http://archive.debian.org/debian-security@g' "$f" || true
    sed -i -E '/stretch-updates/d' "$f" || true
  done
}

fix_ubuntu_eol_sources() {
  # Ubuntu EOL → old-releases.ubuntu.com
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -f "$f" ] || continue
    sed -i -E 's@https?://([a-z0-9.-]+\.)?ubuntu\.com/?ubuntu@http://old-releases.ubuntu.com/ubuntu@g' "$f" || true
    sed -i -E 's@https?://security\.ubuntu\.com/ubuntu@http://old-releases.ubuntu.com/ubuntu@g' "$f" || true
  done
}

enable_systemd_wants_link() {
  # 不跑 systemctl；仅放 wants 链接，首启自然拉起
  mkdir -p /etc/systemd/system/multi-user.target.wants || true
  if   [ -e /lib/systemd/system/qemu-guest-agent.service ]; then
    ln -sf /lib/systemd/system/qemu-guest-agent.service \
           /etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service || true
  elif [ -e /usr/lib/systemd/system/qemu-guest-agent.service ]; then
    ln -sf /usr/lib/systemd/system/qemu-guest-agent.service \
           /etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service || true
  fi
}

# --- APT 系（Debian/Ubuntu） ---
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  . /etc/os-release 2>/dev/null || true
  low_id="$(printf '%s' "${ID:-}" | tr '[:upper:]' '[:lower:]')"
  case ",$low_id," in
    *,debian,*) fix_debian_eol_sources ;;
    *,ubuntu,*) fix_ubuntu_eol_sources ;;
    *) : ;;
  esac

  apt-get -o Acquire::Check-Valid-Until=false update || true
  if apt-get install -y --no-install-recommends qemu-guest-agent; then
    ok=1
  fi
  enable_systemd_wants_link
fi

# --- DNF / YUM 系（RHEL/CentOS/Alma/Rocky 等） ---
if [ "$ok" -eq 0 ] && command -v dnf >/dev/null 2>&1; then
  dnf -y install qemu-guest-agent && ok=1 || true
  enable_systemd_wants_link
fi
if [ "$ok" -eq 0 ] && command -v yum >/dev/null 2>&1; then
  yum -y install qemu-guest-agent && ok=1 || true
  enable_systemd_wants_link
fi
if [ "$ok" -eq 0 ] && command -v microdnf >/dev/null 2>&1; then
  microdnf -y install qemu-guest-agent && ok=1 || true
  enable_systemd_wants_link
fi

# --- openSUSE ---
if [ "$ok" -eq 0 ] && command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install -y qemu-guest-agent && ok=1 || true
  enable_systemd_wants_link
fi

# --- Arch ---
if [ "$ok" -eq 0 ] && command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm qemu-guest-agent && ok=1 || true
  enable_systemd_wants_link
fi

# --- Alpine（openrc） ---
if [ "$ok" -eq 0 ] && command -v apk >/dev/null 2>&1; then
  apk add --no-cache qemu-guest-agent && ok=1 || true
  if [ "$ok" -eq 1 ] && command -v rc-update >/dev/null 2>&1; then
    rc-update add qemu-guest-agent default 2>/dev/null || true
  fi
fi

if [ "$ok" -eq 0 ]; then
  echo "[WARN] qemu-guest-agent not installed (pkg manager absent or repos unavailable). Skipping." >&2
fi

exit 0
