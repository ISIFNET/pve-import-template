#!/usr/bin/env bash
#
# setup.sh - 为 pve-import-template 准备 Proxmox VE 宿主机环境（幂等，可重复运行）
#
#   * 识别 Debian / PVE 版本（PVE 7 bullseye、PVE 8 bookworm、PVE 9 trixie，均不依赖 lsb_release）
#   * apt 源：无有效订阅时禁用 enterprise 源（pve / ceph）；添加 pve-no-subscription 源
#     （bullseye 用 .list，bookworm/trixie 用 deb822 .sources）；注释掉失效的 cdrom 源
#   * 安装依赖：git python3 python3-tqdm python3-yaml libguestfs-tools unzip xz-utils
#     ca-certificates whiptail curl（仅安装缺失的包；可选 libguestfs-xfs；非 PVE 主机额外 qemu-utils）
#   * 嵌套虚拟化：按 CPU 厂商写入 kvm_intel / kvm_amd 的 nested=1（可用 --no-nested 跳过）
#   * 环境检查：NTP 时间同步、TLS 下载链路、/dev/kvm 与 libguestfs 前置条件、磁盘剩余空间
#
# 用法：
#   ./setup.sh [--skip-repo] [--skip-checks] [--no-nested] [--full-check] [-h]
#
# 环境变量（与同名选项等价）：
#   SKIP_REPO=1     不修改 apt 源（仅安装依赖 + 检查）
#   SKIP_CHECKS=1   跳过时间/TLS/libguestfs 检查
#   SKIP_NESTED=1   不配置嵌套虚拟化
#   FULL_CHECK=1    额外运行 libguestfs-test-tool（约 1-3 分钟）
#   TLS_CHECK_URL   自定义 TLS 检查地址（默认 https://cloud.debian.org/images/cloud/）

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export LC_ALL="${LC_ALL:-C.UTF-8}"

SKIP_REPO="${SKIP_REPO:-0}"
SKIP_CHECKS="${SKIP_CHECKS:-0}"
SKIP_NESTED="${SKIP_NESTED:-0}"
FULL_CHECK="${FULL_CHECK:-0}"

for arg in "$@"; do
    case "$arg" in
        --skip-repo)   SKIP_REPO=1 ;;
        --skip-checks) SKIP_CHECKS=1 ;;
        --no-nested)   SKIP_NESTED=1 ;;
        --full-check)  FULL_CHECK=1 ;;
        -h|--help)
            sed -nE '2,/^[^#]/{/^#/{s/^# ?//;p}}' "$0"
            exit 0 ;;
        *) printf 'ERROR: 未知参数 %s\n' "$arg" >&2; exit 1 ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WARNINGS=()

log()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
note() { WARNINGS+=("$1"); warn "$1"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

if [ "$(id -u)" -ne 0 ]; then
    if have sudo; then
        exec sudo -E bash "$0" "$@"
    fi
    die '请以 root 运行本脚本。'
fi

# ---------------------------------------------------------------------------
# 发行版 / PVE 版本
# ---------------------------------------------------------------------------
os_id=''; codename=''; version_id=''
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    os_id="$(. /etc/os-release && printf '%s' "${ID:-}")"
    # shellcheck disable=SC1091
    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
    # shellcheck disable=SC1091
    version_id="$(. /etc/os-release && printf '%s' "${VERSION_ID:-}")"
fi
if [ -z "$codename" ] && [ -r /etc/debian_version ]; then
    # 例如 "12.7" 或 "trixie/sid"
    case "$(cut -d. -f1 /etc/debian_version)" in
        11) codename=bullseye ;;
        12) codename=bookworm ;;
        13) codename=trixie ;;
        14) codename=forky ;;
        *)  codename="$(sed -nE 's#^([a-z]+)/sid$#\1#p' /etc/debian_version)" ;;
    esac
fi

is_pve=0
have pveversion && is_pve=1
pve_ver=''
if [ "$is_pve" = 1 ]; then
    pve_ver="$(pveversion 2>/dev/null | sed -nE 's#^pve-manager/([0-9.]+).*#\1#p' || true)"
fi
[ -n "$pve_ver" ] || pve_ver="$(dpkg-query -W -f='${Version}' pve-manager 2>/dev/null | cut -d- -f1 || true)"

log "系统：${os_id:-unknown} ${version_id:-} (${codename:-unknown})${pve_ver:+ / Proxmox VE ${pve_ver}}"
if [ "$is_pve" = 0 ]; then
    note '未检测到 pveversion：本工具面向 Proxmox VE 宿主机，其他 Debian 系主机只安装依赖。'
fi
if ! have apt-get; then
    die '未找到 apt-get。本脚本仅支持 Debian / Proxmox VE，其他系统请手动安装：python3-yaml python3-tqdm libguestfs-tools unzip xz-utils'
fi
case "$codename" in
    bullseye|bookworm|trixie) ;;
    '') note '无法识别 Debian 代号，跳过 apt 源修改。'; SKIP_REPO=1 ;;
    *)  note "未经测试的 Debian 版本：${codename}，继续尝试。" ;;
esac

# ---------------------------------------------------------------------------
# apt 源
# ---------------------------------------------------------------------------
enabled_repos() {
    # 已启用的源（同时覆盖 .list 与 deb822 .sources；Enabled: false 的会被排除）
    apt-get indextargets --no-release-info --format '$(REPO_URI) $(COMPONENT)' 2>/dev/null | sort -u || true
}

subscription_active() {
    have pvesubscription || return 1
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

disable_cdrom_repos() {
    local f
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [ -f "$f" ] || continue
        grep -qE '^[[:space:]]*deb[[:space:]]+cdrom:' "$f" || continue
        log "注释掉失效的 cdrom 源：$f"
        sed -i -E 's|^([[:space:]]*deb[[:space:]]+cdrom:.*)$|# \1|' "$f"
    done
}

fetch() {
    # fetch <url> <dest>
    if have curl; then
        curl -fsL --retry 3 -o "$2" "$1"
    elif have wget; then
        wget -q -O "$2" "$1"
    else
        return 1
    fi
}

KEYRING=''
find_proxmox_keyring() {
    # 把可用的 Proxmox 仓库密钥路径写入 KEYRING；缺失时尝试下载
    local k
    KEYRING=''
    for k in /usr/share/keyrings/proxmox-archive-keyring.gpg \
             "/etc/apt/trusted.gpg.d/proxmox-release-${codename}.gpg" \
             "/usr/share/keyrings/proxmox-release-${codename}.gpg"; do
        [ -s "$k" ] && { KEYRING="$k"; return 0; }
    done
    k="/usr/share/keyrings/proxmox-release-${codename}.gpg"
    log "未找到 Proxmox 仓库密钥，尝试下载：$k"
    if fetch "https://enterprise.proxmox.com/debian/proxmox-release-${codename}.gpg" "$k" && [ -s "$k" ]; then
        chmod 0644 "$k"
        KEYRING="$k"
        return 0
    fi
    rm -f "$k"
    return 1
}

add_no_subscription_repo() {
    local keyring=''
    if enabled_repos | grep -qE 'download\.proxmox\.com/debian/pve/? pve-no-subscription$'; then
        log 'pve-no-subscription 源已存在，跳过添加。'
        return
    fi
    if grep -rqsE '^[[:space:]]*deb[[:space:]].*download\.proxmox\.com/debian/pve.*pve-no-subscription' \
            /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
        log 'pve-no-subscription 源已存在（.list），跳过添加。'
        return
    fi
    find_proxmox_keyring || note '未找到 Proxmox 仓库密钥且下载失败，apt update 可能报 NO_PUBKEY。'
    keyring="$KEYRING"

    case "$codename" in
        bullseye)
            # PVE 7 使用传统 .list 格式
            local repo_file=/etc/apt/sources.list.d/pve-no-subscription.list
            log "添加 pve-no-subscription 源（${codename}）：$repo_file"
            if [ -n "$keyring" ]; then
                echo "deb [signed-by=${keyring}] http://download.proxmox.com/debian/pve ${codename} pve-no-subscription" > "$repo_file"
            else
                echo "deb http://download.proxmox.com/debian/pve ${codename} pve-no-subscription" > "$repo_file"
            fi
            ;;
        *)
            local repo_file=/etc/apt/sources.list.d/pve-no-subscription.sources
            log "添加 pve-no-subscription 源（${codename}）：$repo_file"
            {
                echo 'Types: deb'
                echo 'URIs: http://download.proxmox.com/debian/pve'
                echo "Suites: ${codename}"
                echo 'Components: pve-no-subscription'
                [ -n "$keyring" ] && echo "Signed-By: ${keyring}"
            } > "$repo_file"
            ;;
    esac
}

if [ "$SKIP_REPO" = 1 ]; then
    log 'SKIP_REPO=1：不修改 apt 源。'
else
    disable_cdrom_repos
    if [ "$is_pve" = 1 ]; then
        if subscription_active; then
            log '检测到有效 Proxmox 订阅，保留 enterprise 源。'
        elif enabled_repos | grep -q 'enterprise\.proxmox\.com'; then
            log '未检测到有效订阅，禁用 enterprise 源以免 apt update 报 401。'
            disable_enterprise_repos
        fi
        add_no_subscription_repo
    fi
fi

# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------
apt_update() {
    local out
    log 'apt-get update'
    if apt-get update -o Acquire::Retries=3; then
        return 0
    fi
    warn 'apt-get update 有错误，尝试诊断：'
    out="$(apt-get update -o Acquire::Retries=1 2>&1 | grep -E '^(E|W):' || true)"
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    case "$out" in
        *401*) warn '  → enterprise 源需要订阅：./setup.sh 会在无订阅时自动禁用；或手动编辑 /etc/apt/sources.list.d/*enterprise*' ;;
    esac
    case "$out" in
        *NO_PUBKEY*|*"not signed"*) warn '  → 缺少仓库密钥：apt-get install proxmox-archive-keyring 或检查 Signed-By 路径' ;;
    esac
    case "$out" in
        *"Could not resolve"*|*"Temporary failure"*) warn '  → DNS/网络不通：检查 /etc/resolv.conf 与代理设置' ;;
    esac
    warn '  将继续尝试安装（可能使用旧的索引）。'
    return 1
}

pkg_installed() {
    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'
}

packages=(ca-certificates git python3 python3-tqdm python3-yaml libguestfs-tools unzip xz-utils whiptail curl)
if [ "$is_pve" = 0 ] && ! have qemu-img; then
    packages+=(qemu-utils)
fi

missing=()
for p in "${packages[@]}"; do
    pkg_installed "$p" || missing+=("$p")
done

# 可选包：缺失不致命（libguestfs-xfs 用于 RHEL 系 XFS 根分区的镜像；部分版本已并入 libguestfs-tools）
optional_packages=(libguestfs-xfs)
optional_missing=()
for p in "${optional_packages[@]}"; do
    pkg_installed "$p" || optional_missing+=("$p")
done

if [ "${#missing[@]}" -eq 0 ] && [ "${#optional_missing[@]}" -eq 0 ]; then
    log "依赖已全部安装：${packages[*]} ${optional_packages[*]}"
else
    apt_update || true
    if [ "${#missing[@]}" -gt 0 ]; then
        log "安装依赖：${missing[*]}"
        if ! apt-get install -y "${missing[@]}"; then
            warn '批量安装失败，改为逐个安装以定位问题。'
            failed=()
            for p in "${missing[@]}"; do
                apt-get install -y "$p" || failed+=("$p")
            done
            if [ "${#failed[@]}" -gt 0 ]; then
                die "以下软件包安装失败：${failed[*]}。请先修复 apt 源（apt-get update）后重试。"
            fi
        fi
    fi
    for p in "${optional_missing[@]}"; do
        if apt-cache show "$p" >/dev/null 2>&1; then
            log "安装可选依赖：$p"
            apt-get install -y "$p" || note "可选包 $p 安装失败（RHEL 系 XFS 镜像定制可能受影响）。"
        else
            info "可选包 $p 在当前版本不可用，跳过。"
        fi
    done
fi

# 确保 CA 证书链是最新的（过期/缺失的根证书会导致下载时 CERTIFICATE_VERIFY_FAILED）
update-ca-certificates >/dev/null 2>&1 || true

# Python 模块与关键命令最终确认
if ! python3 -c 'import yaml, tqdm' 2>/dev/null; then
    warn 'python3 无法导入 yaml / tqdm，尝试用 pip 安装。'
    if python3 -m pip --version >/dev/null 2>&1; then
        python3 -m pip install --break-system-packages pyyaml tqdm 2>/dev/null \
            || python3 -m pip install pyyaml tqdm \
            || die 'pip 安装 pyyaml / tqdm 失败。'
    else
        die 'python3-yaml / python3-tqdm 不可用，请检查 python3 版本与 apt 源。'
    fi
fi
for cmd in virt-customize xz unzip; do
    have "$cmd" || die "安装后仍未找到命令：$cmd"
done

# ---------------------------------------------------------------------------
# 嵌套虚拟化（按 CPU 厂商选择模块；幂等）
# ---------------------------------------------------------------------------
setup_nested() {
    local kvm_mod='' param
    if grep -qi 'vendor_id.*AuthenticAMD' /proc/cpuinfo; then
        kvm_mod=kvm_amd
    elif grep -qi 'vendor_id.*GenuineIntel' /proc/cpuinfo; then
        kvm_mod=kvm_intel
    fi
    if [ -z "$kvm_mod" ] || ! grep -qE '(vmx|svm)' /proc/cpuinfo; then
        info 'CPU 不支持硬件虚拟化或无法识别厂商，跳过嵌套虚拟化配置。'
        return
    fi
    param="/sys/module/${kvm_mod}/parameters/nested"
    if ! grep -qs "^options ${kvm_mod} nested=1" /etc/modprobe.d/*.conf 2>/dev/null; then
        if echo "options ${kvm_mod} nested=1" 2>/dev/null >> /etc/modprobe.d/kvm-nested.conf; then
            info "已写入 /etc/modprobe.d/kvm-nested.conf（${kvm_mod} nested=1）"
        else
            note '无法写入 /etc/modprobe.d/kvm-nested.conf，跳过嵌套虚拟化配置。'
            return
        fi
    fi
    if [ -r "$param" ] && grep -qE '^(Y|1)$' "$param"; then
        info "嵌套虚拟化已启用（${kvm_mod}）。"
        return
    fi
    if pgrep -x kvm >/dev/null 2>&1 || pgrep -x qemu-system-x86_64 >/dev/null 2>&1; then
        note "有 VM 正在运行，无法重载 ${kvm_mod}；嵌套虚拟化将在下次重启后生效。"
        return
    fi
    modprobe -r "$kvm_mod" 2>/dev/null || true
    modprobe "$kvm_mod" nested=1 2>/dev/null || true
    if [ -r "$param" ] && grep -qE '^(Y|1)$' "$param"; then
        info "嵌套虚拟化已启用（${kvm_mod}）。"
    else
        note "未能立即启用嵌套虚拟化（${kvm_mod}），重启后生效。"
    fi
}

if [ "$SKIP_NESTED" = 1 ]; then
    log 'SKIP_NESTED=1：跳过嵌套虚拟化配置。'
else
    log '嵌套虚拟化'
    setup_nested
fi

# ---------------------------------------------------------------------------
# 环境检查（不阻断安装，只给出可操作的提示）
# ---------------------------------------------------------------------------
check_time_sync() {
    local synced
    synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    if [ "$synced" != 'yes' ] && have chronyc; then
        # chrony 已安装但尚未同步：尝试立即校时（失败无副作用）
        chronyc makestep >/dev/null 2>&1 || true
        sleep 1
        synced="$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)"
    fi
    if [ "$synced" = 'yes' ]; then
        log "系统时间已同步：$(date -R)"
        return
    fi
    note "系统时间未通过 NTP 同步（当前：$(date -R)）。时间偏差会导致下载镜像时报 certificate verify failed。"
    if have chronyc; then
        warn '  处理：systemctl enable --now chrony && chronyc makestep && chronyc tracking'
    elif have timedatectl; then
        warn '  处理：apt-get install -y chrony（或 timedatectl set-ntp true）'
    fi
}

check_tls() {
    local url="${TLS_CHECK_URL:-https://cloud.debian.org/images/cloud/}"
    local err proxy
    proxy="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-${HTTP_PROXY:-}}}}"
    log "检查 TLS 下载链路：$url${proxy:+（经由代理 $proxy）}"
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
        info 'OK'
        return
    fi
    note "TLS/网络检查失败：$err"
    case "$err" in
        *CERTIFICATE_VERIFY_FAILED*)
            warn '  证书校验失败，常见原因与处理：'
            warn '    1) 系统时间错误            → date -R；chronyc makestep'
            warn '    2) CA 证书过期/缺失        → apt-get install --reinstall ca-certificates && update-ca-certificates'
            warn '    3) 透明代理/公司代理拦截 HTTPS → 检查 http_proxy/https_proxy 与 /etc/apt/apt.conf.d 代理设置'
            warn '    4) 仅作为临时手段：python3 import.py ... --insecure（跳过证书校验）'
            ;;
        *"Name or service not known"*|*"Temporary failure"*|*"nodename nor servname"*)
            warn '  DNS 解析失败：检查 /etc/resolv.conf；镜像下载也可用 --mirror 换国内源。'
            ;;
        *)
            warn '  请检查宿主机 DNS/网络/代理配置（http_proxy / https_proxy）。'
            ;;
    esac
}

check_guestfs() {
    log '检查 libguestfs 前置条件'
    if [ -c /dev/kvm ]; then
        info '/dev/kvm 可用（virt-customize 使用 KVM 加速）'
    else
        note '/dev/kvm 不存在：virt-customize 将回退到软件模拟（force_tcg），速度较慢；若 PVE 本身是虚拟机，请在上层开启嵌套虚拟化。'
    fi
    local k bad=0
    for k in /boot/vmlinuz-*; do
        [ -f "$k" ] || continue
        if [ "$(stat -c '%a' "$k" 2>/dev/null || echo 644)" -lt 644 ]; then
            bad=1
        fi
    done
    if [ "$bad" = 1 ]; then
        note '部分 /boot/vmlinuz-* 权限非 0644，libguestfs 可能无法构建 appliance：chmod 0644 /boot/vmlinuz-*'
    fi
    if [ "$FULL_CHECK" = 1 ] && have libguestfs-test-tool; then
        log '运行 libguestfs-test-tool（可能需要 1-3 分钟）'
        if timeout 600 libguestfs-test-tool >/tmp/libguestfs-test-tool.log 2>&1; then
            info 'libguestfs-test-tool: OK'
        else
            note 'libguestfs-test-tool 失败，详见 /tmp/libguestfs-test-tool.log（常见修复：update-guestfs-appliance；/dev/kvm 不可用时脚本会自动回退 force_tcg）。'
        fi
    fi
}

check_disk_space() {
    local dir="$SCRIPT_DIR" avail_kb
    avail_kb="$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print $4}')"
    [ -n "$avail_kb" ] || return 0
    log "镜像缓存目录剩余空间：$((avail_kb / 1024 / 1024)) GiB（$dir/cloud_img）"
    if [ "$avail_kb" -lt $((10 * 1024 * 1024)) ]; then
        note '剩余空间不足 10 GiB：每个云镜像约 0.3-2 GiB，导入全部模板需要更多空间；可把仓库放到更大的分区。'
    fi
}

check_pve_tools() {
    [ "$is_pve" = 1 ] || return 0
    local c missing_tools=()
    for c in qm pvesh pvesm; do
        have "$c" || missing_tools+=("$c")
    done
    if [ "${#missing_tools[@]}" -gt 0 ]; then
        note "缺少 PVE 命令：${missing_tools[*]}（import.py / update-templates.py 需要它们）。"
    fi
}

if [ "$SKIP_CHECKS" = 1 ]; then
    log 'SKIP_CHECKS=1：跳过环境检查。'
else
    check_pve_tools
    check_time_sync
    check_tls
    check_guestfs
    check_disk_space
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
log '环境准备完成。'
if [ "${#WARNINGS[@]}" -gt 0 ]; then
    printf '    需要留意的 %d 项：\n' "${#WARNINGS[@]}"
    for w in "${WARNINGS[@]}"; do
        printf '      - %s\n' "$w"
    done
fi
info '下一步：bash run.sh 启动交互向导；或 python3 import.py --list 查看模板，'
info '        python3 import.py <storage> <start-vmid> --only-new 导入主流系列。'
