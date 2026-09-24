#!/usr/bin/env bash
#
# run.sh - pve-import-template 一键入口
#
#   1) 通过管道运行（curl ... | bash）时：克隆/更新仓库到 INSTALL_DIR，再以仓库内的 run.sh 继续；
#   2) 在仓库内运行时：可选 git pull 自更新 → 依赖缺失时运行 setup.sh → 启动交互向导（tui.py）。
#
# 用法：
#   bash run.sh                                  # 交互向导（默认只勾选主流系列 ubuntu/debian/centos/alpine）
#   bash run.sh local-lvm 9000                   # 向导，预填存储 / 起始 VMID
#   bash run.sh -y [storage] [vmid] [import.py 参数...]
#                                                # 非交互：直接导入（默认主流系列 + --only-new）
#   bash run.sh -y local-lvm 9000 --all          # 非交互导入全部模板
#   bash run.sh update                           # 更新已导入模板的向导（补调优 / qga / root 登录 / 换源）
#   bash run.sh -y update --all --qga            # 非交互：等价 python3 update-templates.py --all --qga -y
#   curl -fsSL <run.sh 地址> | bash -s -- -y local-lvm 9000
#
# 选项：
#   -y, --yes        非交互模式（无终端时自动启用）
#   --text           向导使用纯文本界面（不用 whiptail）
#   --setup-only     只准备环境（setup.sh），不导入
#   -h, --help       显示帮助
#
# 环境变量：
#   INSTALL_DIR      管道模式下的克隆目录（默认 $PWD/pve-import-template）
#   REPO_URL         仓库地址（默认 https://github.com/ISIFNET/pve-import-template.git）
#   NO_UPDATE=1      不执行 git pull 自更新
#   SKIP_SETUP=1     不运行 setup.sh；FORCE_SETUP=1 即使依赖齐全也运行
#   NONINTERACTIVE=1 等同于 -y

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/ISIFNET/pve-import-template.git}"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARN: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    # 打印文件头部的注释块作为帮助
    local self="${BASH_SOURCE[0]:-$0}"
    if [ -f "$self" ]; then
        sed -nE '2,/^[^#]/{/^#/{s/^# ?//;p}}' "$self"
    else
        echo '用法: bash run.sh [-y] [--text] [--setup-only] [storage] [start-vmid] [import.py 参数...]'
    fi
}

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
noninteractive="${NONINTERACTIVE:-0}"
setup_only=0
text_ui=0
mode=''
positional=()
extra_args=()
for arg in "$@"; do
    case "$arg" in
        -y|--yes)      noninteractive=1 ;;
        --setup-only)  setup_only=1 ;;
        --text)        text_ui=1 ;;
        -h|--help)     usage; exit 0 ;;
        import|update)
            if [ -z "$mode" ] && [ "${#positional[@]}" -eq 0 ]; then
                mode="$arg"
            else
                positional+=("$arg")
            fi
            ;;
        -*)            extra_args+=("$arg") ;;
        *)
            if [ "${#positional[@]}" -lt 3 ] && [ "${#extra_args[@]}" -eq 0 ]; then
                positional+=("$arg")
            else
                extra_args+=("$arg")
            fi
            ;;
    esac
done

# 终端与语言环境：whiptail 需要 UTF-8 locale 才能正确显示中文
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *UTF-8*|*utf8*|*UTF8*|*utf-8*) ;;
    *) export LC_ALL=C.UTF-8 ;;
esac

have_tty() {
    [ -t 0 ] && [ -t 1 ] && return 0
    [ -c /dev/tty ] && { true < /dev/tty; } 2>/dev/null
}

# ---------------------------------------------------------------------------
# 自举：不在仓库内（如 curl | bash）时先克隆/更新，再交给仓库内的 run.sh
# ---------------------------------------------------------------------------
script_path="${BASH_SOURCE[0]:-}"
script_dir=''
if [ -n "$script_path" ] && [ -f "$script_path" ]; then
    script_dir="$(cd "$(dirname "$script_path")" && pwd)"
fi

if [ -z "$script_dir" ] || [ ! -f "$script_dir/import.py" ]; then
    install_dir="${INSTALL_DIR:-$PWD/pve-import-template}"
    if ! command -v git >/dev/null 2>&1; then
        log '安装 git'
        export DEBIAN_FRONTEND=noninteractive
        apt-get update || true      # 未订阅的 enterprise 源会让 update 返回非 0，这里容忍
        apt-get install -y git
    fi
    if [ -d "$install_dir/.git" ]; then
        log "更新仓库：$install_dir"
        git -C "$install_dir" pull --ff-only || warn 'git pull 失败，使用本地已有版本继续。'
    else
        log "克隆仓库到：$install_dir"
        git clone "$REPO_URL" "$install_dir"
    fi
    export NO_UPDATE=1
    if have_tty && ! [ -t 0 ]; then
        exec bash "$install_dir/run.sh" "$@" < /dev/tty
    fi
    exec bash "$install_dir/run.sh" "$@"
fi

cd "$script_dir"

# ---------------------------------------------------------------------------
# root
# ---------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        log '需要 root 权限，改用 sudo 重新运行'
        exec sudo -E NO_UPDATE="${NO_UPDATE:-0}" bash "$script_dir/run.sh" "$@"
    fi
    die '请以 root 运行。'
fi

# ---------------------------------------------------------------------------
# 自更新（仓库干净且允许时 git pull；run.sh 自身有更新则重新执行）
# ---------------------------------------------------------------------------
if [ "${NO_UPDATE:-0}" != 1 ] && [ -d .git ] && command -v git >/dev/null 2>&1; then
    if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
        warn '仓库有本地修改，跳过 git pull（NO_UPDATE=1 可关闭此提示）。'
    else
        before="$(git rev-parse HEAD 2>/dev/null || true)"
        log '检查更新（git pull --ff-only）'
        if git pull --ff-only; then
            after="$(git rev-parse HEAD 2>/dev/null || true)"
            if [ -n "$before" ] && [ "$before" != "$after" ] && ! git diff --quiet "$before" "$after" -- run.sh; then
                log 'run.sh 已更新，重新执行新版本'
                export NO_UPDATE=1
                exec bash "$script_dir/run.sh" "$@"
            fi
        else
            warn 'git pull 失败（网络问题？），使用本地版本继续。'
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------
deps_ready() {
    command -v virt-customize >/dev/null 2>&1 || return 1
    command -v xz >/dev/null 2>&1 || return 1
    command -v unzip >/dev/null 2>&1 || return 1
    python3 -c 'import yaml, tqdm' >/dev/null 2>&1 || return 1
    return 0
}

if [ "${SKIP_SETUP:-0}" = 1 ]; then
    log 'SKIP_SETUP=1：跳过 setup.sh'
elif [ "${FORCE_SETUP:-0}" = 1 ] || [ "$setup_only" = 1 ] || ! deps_ready; then
    bash ./setup.sh
else
    log '依赖已就绪，跳过 setup.sh（FORCE_SETUP=1 可强制重新检查环境）'
fi

if [ "$setup_only" = 1 ]; then
    log '环境准备完成。运行 bash run.sh 启动导入向导，或 python3 import.py --list 查看模板。'
    exit 0
fi

command -v qm >/dev/null 2>&1 || die '未找到 qm：本工具需要在 Proxmox VE 宿主机上运行。'

# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------
tui_args=()
[ -n "$mode" ] && tui_args+=("$mode")
if [ "$noninteractive" != 1 ]; then
    if [ "$text_ui" = 1 ]; then
        log '启动交互向导（纯文本界面）'
        exec python3 ./tui.py "${tui_args[@]}" --text "${positional[@]}" "${extra_args[@]}"
    elif have_tty; then
        log '启动交互向导（非交互模式：bash run.sh -y [storage] [vmid] ...）'
        if [ -t 0 ]; then
            exec python3 ./tui.py "${tui_args[@]}" "${positional[@]}" "${extra_args[@]}"
        fi
        exec python3 ./tui.py "${tui_args[@]}" "${positional[@]}" "${extra_args[@]}" < /dev/tty
    else
        log '未检测到终端，改为非交互模式'
    fi
fi

if [ "$mode" = update ]; then
    log "非交互更新：python3 update-templates.py ${positional[*]} ${extra_args[*]} -y"
    exec python3 ./update-templates.py "${positional[@]}" "${extra_args[@]}" -y
fi

storage="${positional[0]:-local-lvm}"
start_vmid="${positional[1]:-9000}"
import_args=()
[ "${#positional[@]}" -ge 3 ] && import_args+=("${positional[2]}")
import_args+=("${extra_args[@]}")
only_new=1
for arg in "${import_args[@]}"; do
    [ "$arg" = '--only-new' ] && only_new=0
done
[ "$only_new" = 1 ] && import_args+=('--only-new')

log "非交互导入：storage=${storage} start-vmid=${start_vmid} ${import_args[*]}"
echo '    （不指定模板时只导入主流系列；--all 导入全部；python3 import.py --list 查看模板）'
exec python3 ./import.py "$storage" "$start_vmid" "${import_args[@]}"
