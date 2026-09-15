#!/usr/bin/env bash
#
# setup.sh - 为 pve-import-template 准备 Proxmox VE 宿主机环境
#
#   * 添加 pve-no-subscription 源（PVE 8 / bookworm 与 PVE 9+ / trixie，统一 deb822 格式）
#   * 无有效订阅时禁用 enterprise 源（否则 PVE 9 的 apt update 会因 401 直接失败）
#   * 安装依赖：git python3-tqdm python3-yaml libguestfs-tools unzip xz-utils ca-certificates
#   * 检查时间同步与 TLS 证书链（镜像下载 CERTIFICATE_VERIFY_FAILED 的常见原因）
#
# 环境变量：
#   SKIP_REPO=1     不修改 apt 源（仅安装依赖 + 检查）
#   SKIP_CHECKS=1   跳过时间/TLS 检查
#   TLS_CHECK_URL   自定义 TLS 检查地址（默认 https://cloud.debian.org/images/cloud/）

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die '请以 root 运行本脚本。'

if ! command -v pveversion >/dev/null 2>&1; then
    warn '未检测到 pveversion：本脚本面向 Proxmox VE 宿主机，其他系统仅尝试安装依赖。'
fi

# ---------------------------------------------------------------------------
# 发行版信息（PVE 9 默认不带 lsb_release，改读 /etc/os-release）
# ---------------------------------------------------------------------------
codename=''
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
fi
[ -n "$codename" ] || die '无法从 /etc/os-release 读取 VERSION_CODENAME。'

pve_ver="$(pveversion 2>/dev/null | sed -nE 's#^pve-manager/([0-9.]+).*#\1#p' || true)"
log "Debian ${codename}${pve_ver:+ / Proxmox VE ${pve_ver}}"

case "$codename" in
    bookworm|trixie) ;;
    *) warn "未经测试的 Debian 版本：${codename}，继续尝试。" ;;
esac

# ---------------------------------------------------------------------------
# apt 源
# ---------------------------------------------------------------------------
enabled_repos() {
    # 列出所有已启用的源（同时覆盖 .list 与 deb822 .sources、Enabled: false 的会被排除）
    apt-get indextargets --no-release-info --format '$(REPO_URI) $(COMPONENT)' 2>/dev/null | sort -u || true
}

subscription_active() {
    command -v pvesubscription >/dev/null 2>&1 || return 1
    pvesubscription get 2>/dev/null | grep -qiE '^status:[[:space:]]*active'
}

disable_enterprise_repos() {
    local f
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        grep -qE '^[[:space:]]*deb[[:space:]].*enterprise\.proxmox\.com' "$f" || continue
        log "禁用 enterprise 源：$f"
        sed -i -E 's|^([[:space:]]*deb[[:space:]].*enterprise\.proxmox\.com.*)$|# \1|' "$f"
    done
    for f in /etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        grep -q 'enterprise\.proxmox\.com' "$f" || continue
        grep -qiE '^Enabled:[[:space:]]*(false|no)' "$f" && continue
        log "禁用 enterprise 源：$f"
        if grep -qiE '^Enabled:' "$f"; then
            sed -i -E 's/^Enabled:.*/Enabled: false/I' "$f"
        else
            sed -i -E '/^Types:/a Enabled: false' "$f"
        fi
    done
}

add_no_subscription_repo() {
    local repo_file=/etc/apt/sources.list.d/pve-no-subscription.sources
    local keyring=/usr/share/keyrings/proxmox-archive-keyring.gpg

    if enabled_repos | grep -qE 'download\.proxmox\.com/debian/pve/? pve-no-subscription$'; then
        log 'pve-no-subscription 源已存在，跳过添加。'
        return
    fi

    log "添加 pve-no-subscription 源（${codename}）：$repo_file"
    {
        echo 'Types: deb'
        echo 'URIs: http://download.proxmox.com/debian/pve'
        echo "Suites: ${codename}"
        echo 'Components: pve-no-subscription'
        if [ -f "$keyring" ]; then
            echo "Signed-By: ${keyring}"
        else
            warn "未找到 ${keyring}，将依赖 /etc/apt/trusted.gpg.d 中的 Proxmox 密钥。"
        fi
    } > "$repo_file"
}

if [ "${SKIP_REPO:-0}" = 1 ]; then
    log 'SKIP_REPO=1：不修改 apt 源。'
elif command -v pveversion >/dev/null 2>&1; then
    if subscription_active; then
        log '检测到有效 Proxmox 订阅，保留 enterprise 源。'
    elif enabled_repos | grep -q 'enterprise\.proxmox\.com'; then
        log '未检测到有效订阅，禁用 enterprise 源以免 apt update 报 401。'
        disable_enterprise_repos
    fi
    add_no_subscription_repo
fi

# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
log 'apt-get update'
apt-get update

log '安装依赖'
apt-get install -y \
    ca-certificates \
    git \
    python3-tqdm \
    python3-yaml \
    libguestfs-tools \
    unzip \
    xz-utils

# 确保 CA 证书链是最新的（过期/缺失的根证书会导致下载时 CERTIFICATE_VERIFY_FAILED）
update-ca-certificates >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
# 环境检查（不阻断安装，只给出可操作的提示）
# ---------------------------------------------------------------------------
check_time_sync() {
    local synced
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    if [ "$synced" = 'yes' ]; then
        log "系统时间已同步：$(date -R)"
        return
    fi
    warn "系统时间未通过 NTP 同步（当前：$(date -R)）。"
    warn '  时间偏差会导致下载镜像时报 certificate verify failed / certificate has expired。'
    warn '  处理：systemctl enable --now chrony && chronyc makestep && chronyc tracking'
}

check_tls() {
    local url="${TLS_CHECK_URL:-https://cloud.debian.org/images/cloud/}"
    local err
    log "检查 TLS 下载链路：$url"
    if err="$(python3 - "$url" <<'PY' 2>&1
import sys, urllib.request
req = urllib.request.Request(sys.argv[1], method='HEAD')
try:
    urllib.request.urlopen(req, timeout=20).close()
except Exception as exc:
    print(exc)
    sys.exit(1)
PY
    )"; then
        echo '    OK'
        return
    fi
    warn "TLS/网络检查失败：$err"
    case "$err" in
        *CERTIFICATE_VERIFY_FAILED*)
            warn '  证书校验失败，常见原因与处理：'
            warn "    1) 系统时间错误            → date -R；chronyc makestep"
            warn '    2) CA 证书过期/缺失        → apt-get install --reinstall ca-certificates && update-ca-certificates'
            warn '    3) 透明代理/公司代理拦截 HTTPS → 检查 http_proxy/https_proxy 与 /etc/apt/apt.conf.d 代理设置'
            warn '    4) 仅作为临时手段：python3 import.py ... --insecure（跳过证书校验）'
            ;;
        *)
            warn '  请检查宿主机 DNS/网络/代理配置（http_proxy / https_proxy）。'
            ;;
    esac
}

if [ "${SKIP_CHECKS:-0}" = 1 ]; then
    log 'SKIP_CHECKS=1：跳过时间/TLS 检查。'
else
    check_time_sync
    check_tls
fi

log '完成。下一步：python3 import.py --list 查看模板，或 python3 import.py <storage> <start-vmid> --only-new'
