#!/bin/sh
# upgrade-packages.sh —— 在镜像内把系统包升级到当前发行版分支的最新版本（拿到安全补丁）
#
# 与其它注入脚本不同，这个脚本**失败就返回非 0**：升级没成功的镜像不能顶替在役模板
# （sync-templates.py 会保留旧模板、把这一次记为失败）。
#
# EOL 发行版：先按镜像自带的源更新；只有源确实不可用（仓库已归档、Release 404）时才切到
# archive.debian.org / old-releases.ubuntu.com，并且只有这种情况才关闭 Valid-Until 校验。
set -u

log() { echo "[upgrade] $*"; }

# 镜像站同步窗口里偶发 Hash Sum mismatch / 连接重置：重试两次再判失败（否则一次抖动就要退避一天）
retry() {
  n=1
  while :; do
    "$@" && return 0
    [ "$n" -ge 3 ] && return 1
    log "attempt $n failed: $*; retrying in $((n * 10))s"
    sleep $((n * 10))
    n=$((n + 1))
  done
}

if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi
ID_LOWER="$(printf '%s' "${ID:-}" | tr '[:upper:]' '[:lower:]')"

apt_sources() {
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$f" ] && echo "$f"
  done
}

apt_switch_to_archive() {
  case "$ID_LOWER" in
    debian)
      for f in $(apt_sources); do
        sed -i -E 's@https?://(deb|ftp\.[a-z]+)\.debian\.org/debian-security@http://archive.debian.org/debian-security@g' "$f"
        sed -i -E 's@https?://security\.debian\.org/debian-security@http://archive.debian.org/debian-security@g' "$f"
        sed -i -E 's@https?://(deb|ftp\.[a-z]+)\.debian\.org/debian@http://archive.debian.org/debian@g' "$f"
        # 归档里没有 *-updates。.list 一行一个源，整行删；deb822 的 .sources 是「Suites: a a-updates」，只删那个词，
        # 整行删会把 stanza 删坏
        case "$f" in
          *.sources) sed -i -E '/^Suites:/s/ [A-Za-z0-9]+-updates//g' "$f" ;;
          *) sed -i -E '/-updates/d' "$f" ;;
        esac
      done
      if [ -d /etc/apt/mirrors ]; then
        for m in /etc/apt/mirrors/*.list; do
          [ -f "$m" ] || continue
          case "$m" in
            *debian-security.list) echo "http://archive.debian.org/debian-security" > "$m" ;;
            *debian.list) echo "http://archive.debian.org/debian" > "$m" ;;
          esac
        done
      fi
      ;;
    ubuntu)
      for f in $(apt_sources); do
        sed -i -E 's@https?://([a-z0-9.-]+\.)?archive\.ubuntu\.com/ubuntu@http://old-releases.ubuntu.com/ubuntu@g' "$f"
        sed -i -E 's@https?://security\.ubuntu\.com/ubuntu@http://old-releases.ubuntu.com/ubuntu@g' "$f"
      done
      ;;
    *) return 1 ;;
  esac
  mkdir -p /etc/apt/apt.conf.d
  # 归档仓库的 Release 早已过期，不关就永远 update 不了；只在确实切到归档源时写入
  printf 'Acquire::Check-Valid-Until "false";\n' > /etc/apt/apt.conf.d/99pit-archive-valid-until
  log "release is end-of-life: switched apt sources to the archive"
}

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  # 不加 Always-Include-Phased-Updates：Ubuntu 分阶段推送的更新正是还在观察期的版本，模板会发给每一台新 VM，
  # 不该比普通机器更早吃到可能被撤回的包
  APT_OPTS="-y -o Acquire::Retries=3 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -o APT::Install-Recommends=false"
  if ! retry apt-get -o Acquire::Retries=3 update; then
    apt_switch_to_archive || { log "apt-get update failed"; exit 1; }
    apt-get -o Acquire::Retries=3 update || { log "apt-get update failed even against the archive"; exit 1; }
  fi
  # shellcheck disable=SC2086
  retry apt-get $APT_OPTS dist-upgrade || { log "apt-get dist-upgrade failed"; exit 1; }
  # shellcheck disable=SC2086
  apt-get $APT_OPTS autoremove --purge || true
  log "apt upgrade done"
  exit 0
fi

# EL 的镜像站同样有同步窗口（repomd 与包不一致 / 连接重置）：和 apt 一样重试两次再判失败
if command -v dnf >/dev/null 2>&1; then
  retry dnf -y upgrade --refresh || { log "dnf upgrade failed"; exit 1; }
  log "dnf upgrade done"
  exit 0
fi
if command -v yum >/dev/null 2>&1; then
  retry yum -y update || { log "yum update failed"; exit 1; }
  log "yum update done"
  exit 0
fi
if command -v microdnf >/dev/null 2>&1; then
  retry microdnf -y upgrade || { log "microdnf upgrade failed"; exit 1; }
  exit 0
fi

if command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive --gpg-auto-import-keys refresh || { log "zypper refresh failed"; exit 1; }
  zypper --non-interactive update -y --auto-agree-with-licenses || { log "zypper update failed"; exit 1; }
  log "zypper update done"
  exit 0
fi

if command -v apk >/dev/null 2>&1; then
  retry apk update || { log "apk update failed"; exit 1; }
  retry apk upgrade || { log "apk upgrade failed"; exit 1; }
  log "apk upgrade done"
  exit 0
fi

if command -v pacman >/dev/null 2>&1; then
  # cloud 镜像的 keyring 在首次开机由 pacman-init 生成；这里临时生成一份，finalize-image.sh 会删掉，
  # 否则所有克隆共享同一把本地主密钥
  if [ ! -d /etc/pacman.d/gnupg ] || [ -z "$(ls -A /etc/pacman.d/gnupg 2>/dev/null)" ]; then
    pacman-key --init && pacman-key --populate archlinux || { log "pacman keyring init failed"; exit 1; }
    : > /etc/pacman.d/.pit-remove-gnupg
  fi
  pacman -Syu --noconfirm || { log "pacman -Syu failed"; exit 1; }
  log "pacman upgrade done"
  exit 0
fi

log "no supported package manager found; nothing upgraded"
exit 0
