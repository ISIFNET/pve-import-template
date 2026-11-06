#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import.py - Proxmox VE Cloud Image Importer

功能亮点
- 仅导入“新增模板”：--only-new
- 选择性导入：支持逗号分隔或通配（glob），例如：'ubuntu-*'
- 下载缓存：按 URL + ETag/Last-Modified 判断是否变化；缓存镜像保持「上游原样」，定制只作用在副本上
- 上游校验：模板配置了 checksum 时下载后核对 SHA256/SHA512；配置了 discover 时自动找最新点版本
- 离线定制（virt-customize）：换源 → 包升级 → 模板 customize → 网络调优 → 注入校验 → 清理（sysprep）
- 模板 VM 带 tags（pit / pit-bYYYYMMDD）与 description 里的构建元数据，供 sync-templates.py 与平台识别
- 默认导入 templates.yaml 的 default_profile 配置组（standard：在售发行版的受支持版本，不含 EOL）；--all 导入全部；
  选择器支持模板名 / glob / 系列名 / @profile（小硬盘 @minimal）
- --bridge / --memory / --cores 自定义模板 VM 默认硬件；--dry-run 仅打印导入计划

用法
  python3 import.py <storage-name> <start-vmid> [template-name[,name2|glob|family]] [options]

示例
  python3 import.py local-lvm 9001
  python3 import.py local-lvm 9001 --all
  python3 import.py local-lvm 9001 --only-new
  python3 import.py local-lvm 9001 @minimal
  python3 import.py local-lvm 9001 ubuntu-22.04,ubuntu-20.04
  python3 import.py local-lvm 9001 'ubuntu-*'
  python3 import.py local-lvm 9001 rocky,alpine
  python3 import.py local-lvm 9001 ubuntu-22.04 --refresh
  python3 import.py local-lvm 9001 --mirror aliyun

已导入的模板要「换成新构建」请用 sync-templates.py（新建 → 切换 → 回收旧模板），不要删了重导。
"""

import sys
import os
import atexit
import contextlib
import ssl
import shutil
import time
import urllib.error
import urllib.request
import subprocess
import json
import fnmatch
import re
import tempfile
from typing import Tuple, Optional

try:
    import yaml
except ImportError:
    print('Some dependencies are missing (python3-yaml).')
    print('Please run setup.sh to install them.')
    sys.exit(2)

try:
    import tqdm  # 只用于交互终端的下载进度条；没有也能跑
except ImportError:
    tqdm = None

import pitlib

# 脚本所在目录（保证无论从哪个工作目录调用都能找到 uploads/ 等资源）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(SCRIPT_DIR, 'VERSION'), 'r') as _fh:
    TOOL_VERSION = _fh.read().strip()

# 进配方指纹的「构建流程版本」：只在构建步骤 / 注入方式变了时手动加一。不用 TOOL_VERSION——改个文档发版也会让
# 全机队所有模板判 recipe-changed、排着队重建（2026-11-05 审查）
RECIPE_VERSION = '2'
# yaml 条目里不进配方指纹的键：eol（支持期）、display_name（平台显示名）、min_cpu_level（宿主机 CPU 要求）都是元数据，
# 不影响镜像内容
RECIPE_IGNORED_KEYS = ('eol', 'display_name', 'min_cpu_level')


# 网络/内核 sysctl 调优脚本（import 与 update-templates.py 共用，单一数据源）
TUNING_SCRIPT = os.path.join(SCRIPT_DIR, 'uploads', 'apply-net-tuning.sh')
UPGRADE_SCRIPT = os.path.join(SCRIPT_DIR, 'uploads', 'upgrade-packages.sh')
VERIFY_SCRIPT = os.path.join(SCRIPT_DIR, 'uploads', 'verify-image.sh')
FINALIZE_SCRIPT = os.path.join(SCRIPT_DIR, 'uploads', 'finalize-image.sh')
# 写进镜像的构建信息（客户工单里「我这台是哪一版模板装的」一查便知）
BUILD_INFO_PATH = '/etc/pve-template-release'

# 镜像缓存目录：默认在脚本目录下的 cloud_img（不随当前工作目录变化），可用 CLOUD_IMG_DIR 指到更大的分区
CACHE_DIR = os.environ.get('CLOUD_IMG_DIR') or os.path.join(SCRIPT_DIR, 'cloud_img')
DOWNLOAD_RETRIES = 3
RETRIABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
# 单个模板 virt-customize 的墙钟上限（含包升级）；超时杀整组进程、该模板记失败
CUSTOMIZE_TIMEOUT = 3600
# 单个上游镜像的下载上限：Content-Length 是对方说了算，坏掉 / 恶意的镜像站不能把根分区写满
MAX_IMAGE_BYTES = 8 * 1024 ** 3

# 模板名会进 qm --name、缓存文件名、import-from 路径：只许 DNS 名字符
TEMPLATE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,40}$')

# 未在 templates.yaml 里声明 default_profile 时的默认配置组（不指定模板时导入它、向导默认勾选它）
DEFAULT_PROFILE = 'standard'

# 系列显示名（--list / TUI 用）
FAMILY_LABELS = {
    'ubuntu': 'Ubuntu',
    'debian': 'Debian',
    'centos': 'CentOS Stream',
    'rocky': 'Rocky Linux',
    'alma': 'AlmaLinux',
    'fedora': 'Fedora',
    'opensuse': 'openSUSE Leap',
    'arch': 'Arch Linux',
    'alpine': 'Alpine Linux',
}

# 模板 VM 默认硬件（可用 --bridge / --memory / --cores 覆盖）
VM_DEFAULTS = {'bridge': 'vmbr0', 'memory': 512, 'cores': 1, 'cpu': 'host,flags=+aes'}

# 运行期间在宿主机上生成的临时脚本（换源脚本等），结束时统一删除
_TEMP_FILES: list = []


def cleanup_temp_files():
    """删除本进程生成的宿主机临时脚本（import.py / update-templates.py 共用）。"""
    while _TEMP_FILES:
        path = _TEMP_FILES.pop()
        with contextlib.suppress(OSError):
            os.remove(path)


atexit.register(cleanup_temp_files)


def template_family(template: dict) -> str:
    """模板所属系列：优先取 family 键，否则由名称前缀推断（almaLinux-9 → alma）。"""
    fam = template.get('family')
    if fam:
        return str(fam).lower()
    match = re.match(r'[A-Za-z]+', template.get('name', ''))
    fam = match.group(0).lower() if match else 'other'
    if fam.endswith('linux') and fam != 'linux':
        fam = fam[:-len('linux')]
    return fam or 'other'


def default_profile(config: dict) -> str:
    return str(config.get('default_profile') or DEFAULT_PROFILE)


def default_template_names(config: dict) -> list:
    """默认配置组展开成模板名（保持 templates.yaml 顺序）。组没定义时退回全部非 EOL 模板，而不是一个都不导。"""
    tpls = config.get('templates', []) or []
    if default_profile(config) not in (config.get('profiles') or {}):
        return [t['name'] for t in tpls if not pitlib.is_eol(t)]
    return [t['name'] for t in match_templates(tpls, '@' + default_profile(config), config=config)]


def default_families(config: dict) -> list:
    """默认配置组涉及的系列（--list / 向导的「默认」标记用）。"""
    names = set(default_template_names(config))
    fams = []
    for t in config.get('templates', []) or []:
        fam = template_family(t)
        if t['name'] in names and fam not in fams:
            fams.append(fam)
    return fams


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family, family.capitalize())


class DownloadError(RuntimeError):
    """镜像下载失败时提供简明、可操作的错误信息。

    kind：not-found（上游删了 / 改名了，要改 templates.yaml）/ tls / network / checksum / space ——
    sync-templates.py 按它决定退避多久、报给平台的原因。
    """

    def __init__(self, message: str, kind: str = 'network'):
        super().__init__(message)
        self.kind = kind


TLS_HINT = (
    'TLS 证书校验失败，常见原因：宿主机时间不准（date -R / chronyc makestep）、\n'
    'CA 证书过期或缺失（apt-get install --reinstall ca-certificates && update-ca-certificates）、\n'
    '代理拦截 HTTPS（检查 http_proxy/https_proxy）。运行 ./setup.sh 可自动检查；\n'
    '临时绕过可加 --insecure（跳过证书校验，不推荐长期使用）。'
)


def disable_tls_verification():
    """--insecure：为 urllib 安装不校验证书的 HTTPS opener（下载 / HEAD / 目录索引共用）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    print('Warning: --insecure 已启用，将跳过镜像下载的 TLS 证书校验。', file=sys.stderr)


class StorageInfo:
    """保留给旧调用方：只记录存储名（磁盘改用 import-from 导入，不再猜 disk-0 卷名）。"""

    class Base:
        def __init__(self, name) -> None:
            self.name = name

    class Dir(Base):
        pass

    class Raw(Base):
        pass


def run(cmd, **extra_env):
    """统一外部命令执行：打印命令、继承环境、失败抛异常。cmd 必须是 list（不经 shell）。"""
    print(f'# {" ".join(str(c) for c in cmd)}')
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    subprocess.run([str(c) for c in cmd], check=True, env=env)


def check_storage(name: str) -> StorageInfo.Base:
    output = subprocess.check_output(['pvesh', 'get', '/storage', '--output-format=json'])
    for st in json.loads(output):
        if st.get('storage') != name:
            continue
        content = st.get('content', '')
        if 'images' not in content.split(','):
            raise pitlib.PitError(f'PVE storage {name} does not support VM images.')
        if st.get('disable'):
            raise pitlib.PitError(f'PVE storage {name} is disabled.')
        t = st.get('type')
        if t in ['dir', 'nfs', 'glusterfs', 'cifs', 'btrfs']:
            return StorageInfo.Dir(name)
        return StorageInfo.Raw(name)
    raise pitlib.PitError(f'PVE storage {name} does not exist.')


def vm_exists_by_name(name: str) -> bool:
    return name in list_existing_vm_names()


def list_existing_vm_names() -> set:
    try:
        out = subprocess.check_output(['qm', 'list'])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    names = set()
    for line in out.decode(errors='ignore').splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2:
            names.add(cols[1])
    return names


def cluster_vm_index() -> Tuple[set, set]:
    """返回 (已用 VMID 集合 —— 含 LXC, 已存在的 qemu 名称集合)。

    VMID 在 PVE 集群内全局唯一（qemu / lxc 共用号段），漏掉 LXC 会让 qm create 撞号并连锁失败。
    """
    vmids = pitlib.cluster_used_vmids()
    names = set()
    try:
        out = subprocess.check_output(
            ['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'])
        for r in json.loads(out):
            if r.get('type') == 'qemu' and r.get('name'):
                names.add(r['name'])
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        names = list_existing_vm_names()
    return vmids, names


def build_customize_args(customize: Optional[dict]) -> list:
    if not customize:
        return []
    args = []
    # upload：格式 "本地路径:镜像内路径"，本地相对路径基于本脚本目录解析（不依赖当前工作目录）
    for up in customize.get('uploads', []):
        if ':' in up:
            local, remote = up.split(':', 1)
            local = _resolve(local)
            up = f'{local}:{remote}'
        args += ['--upload', up]
    # run：在镜像内执行宿主机上的脚本文件（相对路径基于本脚本目录解析）
    for script in customize.get('run', []):
        args += ['--run', _resolve(script)]
    for cmd in customize.get('commands', []):
        args += ['--run-command', cmd]
    return args


def customize_files(customize: Optional[dict]) -> list:
    """customize 引用到的宿主机文件（进配方指纹：脚本改了 = 需要重建）。"""
    if not customize:
        return []
    files = [_resolve(up.split(':', 1)[0]) for up in customize.get('uploads', []) if ':' in up]
    files += [_resolve(s) for s in customize.get('run', [])]
    return files


def _resolve(path: str) -> str:
    """将相对路径基于脚本所在目录解析为规范化的绝对路径。"""
    if not os.path.isabs(path):
        path = os.path.join(SCRIPT_DIR, path)
    return os.path.normpath(path)


def build_tuning_args() -> list:
    """返回注入网络/内核调优所需的 virt-customize 参数。"""
    if not os.path.exists(TUNING_SCRIPT):
        print(f'Warning: tuning script not found at {TUNING_SCRIPT}, skipping net tuning.', file=sys.stderr)
        return []
    return ['--run', TUNING_SCRIPT]


def detect_os_family(template_name: str) -> Optional[str]:
    """
    根据模板名称检测操作系统类型。
    返回：'debian', 'ubuntu', 'rhel', 'fedora', 'arch', 'alpine', 'suse' 或 None
    """
    name_lower = template_name.lower()
    if 'ubuntu' in name_lower:
        return 'ubuntu'
    if 'debian' in name_lower:
        return 'debian'
    if 'fedora' in name_lower:
        # Fedora 的 repo 结构与 RHEL 系不同，换源规则不通用；单列出来，换源时跳过
        return 'fedora'
    if any(x in name_lower for x in ['centos', 'alma', 'rocky', 'rhel']):
        return 'rhel'
    if 'arch' in name_lower:
        return 'arch'
    if 'alpine' in name_lower:
        return 'alpine'
    if any(x in name_lower for x in ['suse', 'opensuse']):
        return 'suse'
    return None


def detect_rhel_variant(template_name: str) -> str:
    """EL 系模板的发行版：rocky / almalinux / centos-stream；认不出返回 rhel（走通用换源规则）。"""
    name_lower = template_name.lower()
    if 'rocky' in name_lower:
        return 'rocky'
    if 'alma' in name_lower:
        return 'almalinux'
    if 'centos-stream' in name_lower or 'centos' in name_lower:
        return 'centos-stream'
    return 'rhel'


def build_mirror_command(mirror_config: dict, os_family: str) -> Optional[str]:
    """
    根据镜像源配置和操作系统类型，生成配置镜像源的 shell 命令。
    """
    if not mirror_config or not os_family:
        return None

    # 检查该镜像源是否支持此操作系统
    if os_family not in mirror_config:
        return None

    os_mirror = mirror_config[os_family]
    base_url = os_mirror.get('url', '').rstrip('/')

    if not base_url:
        return None

    # 根据不同的操作系统生成不同的配置命令
    if os_family == 'ubuntu':
        return f'''#!/bin/sh
set -eu
# 配置 Ubuntu 镜像源
if [ -f /etc/apt/sources.list ]; then
    cp /etc/apt/sources.list /etc/apt/sources.list.bak
    sed -i -E 's@https?://([a-z0-9.-]+\\.)?archive\\.ubuntu\\.com/ubuntu@{base_url}@g' /etc/apt/sources.list
    sed -i -E 's@https?://([a-z0-9.-]+\\.)?security\\.ubuntu\\.com/ubuntu@{base_url}@g' /etc/apt/sources.list
fi
# Ubuntu 24.04+ 使用 deb822 格式
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
    cp /etc/apt/sources.list.d/ubuntu.sources /etc/apt/sources.list.d/ubuntu.sources.bak
    sed -i -E 's@https?://([a-z0-9.-]+\\.)?archive\\.ubuntu\\.com/ubuntu@{base_url}@g' /etc/apt/sources.list.d/ubuntu.sources
    sed -i -E 's@https?://([a-z0-9.-]+\\.)?security\\.ubuntu\\.com/ubuntu@{base_url}@g' /etc/apt/sources.list.d/ubuntu.sources
fi
exit 0
'''

    elif os_family == 'debian':
        # base_url 本身就带 /debian（如 http://mirrors.aliyun.com/debian）：整段替换 deb.debian.org/debian，
        # 安全源用同级的 <base>-security。旧版直接替换主机名，拼出 .../debian/debian，换源后 apt 全挂。
        return f'''#!/bin/sh
set -eu
# 配置 Debian 镜像源
for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do
    [ -f "$f" ] || continue
    cp "$f" "$f.bak"
    sed -i -E 's@https?://deb\\.debian\\.org/debian-security@{base_url}-security@g' "$f"
    sed -i -E 's@https?://security\\.debian\\.org/debian-security@{base_url}-security@g' "$f"
    sed -i -E 's@https?://deb\\.debian\\.org/debian@{base_url}@g' "$f"
done

# 镜像重定向列表 (Debian 12/13 cloud image 的 mirror+file 引用)
if [ -d /etc/apt/mirrors ]; then
    for mirror_file in /etc/apt/mirrors/*.list; do
        [ -f "$mirror_file" ] || continue
        cp "$mirror_file" "$mirror_file.bak"
        case "$mirror_file" in
            *debian-security.list) echo "{base_url}-security" > "$mirror_file" ;;
            *debian.list) echo "{base_url}" > "$mirror_file" ;;
        esac
    done
fi
exit 0
'''

    elif os_family == 'rhel':
        # RHEL 系（CentOS/AlmaLinux/Rocky）使用 baseurl 替换
        return f'''#!/bin/sh
set -eu
# 配置 RHEL/CentOS 系镜像源
for repo in /etc/yum.repos.d/*.repo; do
    [ -f "$repo" ] || continue
    cp "$repo" "$repo.bak"
    # 注释掉 mirrorlist / metalink，启用 baseurl（AlmaLinux 写成 "# baseurl" 带空格）
    sed -i -E 's/^(mirrorlist|metalink)=/#\\1=/' "$repo"
    sed -i -E 's/^#[[:space:]]*baseurl=/baseurl=/' "$repo"
    sed -i -E 's@https?://mirror\\.centos\\.org@{base_url}@g' "$repo"
    sed -i -E 's@https?://vault\\.centos\\.org@{base_url}@g' "$repo"
    sed -i -E 's@https?://repo\\.almalinux\\.org@{base_url}@g' "$repo"
    sed -i -E 's@https?://dl\\.rockylinux\\.org@{base_url}@g' "$repo"
    sed -i -E 's@https?://mirror\\.rockylinux\\.org@{base_url}@g' "$repo"
done
exit 0
'''

    elif os_family == 'rocky':
        # Rocky：baseurl 写成「#baseurl=http://dl.rockylinux.org/$contentdir/…」（$contentdir = pub/rocky）。
        # 镜像站的目录对应 $contentdir 这一层（如 https://mirrors.aliyun.com/rockylinux），整段替换
        return f'''#!/bin/sh
set -eu
for repo in /etc/yum.repos.d/*.repo; do
    [ -f "$repo" ] || continue
    grep -Eq '^#[[:space:]]*baseurl=https?://dl\\.rockylinux\\.org/\\$contentdir' "$repo" || continue
    cp "$repo" "$repo.bak"
    sed -i -E 's@^(mirrorlist|metalink)=@#\\1=@' "$repo"
    sed -i -E 's@^#[[:space:]]*baseurl=https?://dl\\.rockylinux\\.org/\\$contentdir@baseurl={base_url}@' "$repo"
done
exit 0
'''

    elif os_family == 'almalinux':
        # AlmaLinux：「# baseurl=https://repo.almalinux.org/almalinux/$releasever/…」，镜像站目录对应 /almalinux 这一层
        return f'''#!/bin/sh
set -eu
for repo in /etc/yum.repos.d/*.repo; do
    [ -f "$repo" ] || continue
    grep -Eq '^#[[:space:]]*baseurl=https?://repo\\.almalinux\\.org/almalinux' "$repo" || continue
    cp "$repo" "$repo.bak"
    sed -i -E 's@^(mirrorlist|metalink)=@#\\1=@' "$repo"
    sed -i -E 's@^#[[:space:]]*baseurl=https?://repo\\.almalinux\\.org/almalinux@baseurl={base_url}@' "$repo"
done
exit 0
'''

    elif os_family == 'centos-stream':
        # CentOS Stream 9 / 10 的 repo 文件里只有 metalink、没有 baseurl：按仓库名把 metalink 换成镜像站的 baseurl
        # （$stream = 9-stream / 10-stream，镜像站目录对应 /centos-stream 这一层）。认不出的仓库（debug / source 等，
        # 默认不启用）原样保留
        repos = [('baseos', 'BaseOS'), ('appstream', 'AppStream'), ('crb', 'CRB'),
                 ('highavailability', 'HighAvailability'), ('nfv', 'NFV'), ('rt', 'RT'),
                 ('resilientstorage', 'ResilientStorage')]
        seds = ''.join(
            f"    sed -i -E 's@^metalink=https?://mirrors\\.centos\\.org/metalink\\?repo=centos-{key}-\\$stream&.*$@"
            f"baseurl={base_url}/$stream/{path}/$basearch/os/@' \"$repo\"\n" for key, path in repos)
        seds += (f"    sed -i -E 's@^metalink=https?://mirrors\\.centos\\.org/metalink\\?repo=centos-extras-sig-extras-common-"
                 f"\\$stream&.*$@baseurl={base_url}/SIGs/$stream/extras/$basearch/extras-common/@' \"$repo\"\n")
        return f'''#!/bin/sh
set -eu
for repo in /etc/yum.repos.d/*.repo; do
    [ -f "$repo" ] || continue
    grep -q '^metalink=.*mirrors\\.centos\\.org' "$repo" || continue
    cp "$repo" "$repo.bak"
{seds}done
exit 0
'''

    elif os_family == 'alpine':
        return f'''#!/bin/sh
set -eu
# 配置 Alpine 镜像源
if [ -f /etc/apk/repositories ]; then
    cp /etc/apk/repositories /etc/apk/repositories.bak
    sed -i -E 's@https?://dl-cdn\\.alpinelinux\\.org/alpine@{base_url}@g' /etc/apk/repositories
fi
exit 0
'''

    elif os_family == 'arch':
        return f'''#!/bin/sh
set -eu
# 配置 Arch Linux 镜像源
if [ -f /etc/pacman.d/mirrorlist ]; then
    cp /etc/pacman.d/mirrorlist /etc/pacman.d/mirrorlist.bak
    echo "Server = {base_url}/\\$repo/os/\\$arch" > /etc/pacman.d/mirrorlist
fi
exit 0
'''

    elif os_family == 'suse':
        return f'''#!/bin/sh
set -eu
# 配置 openSUSE 镜像源
zypper mr --disable --all 2>/dev/null || true
zypper ar -fcg {base_url}/distribution/leap/\\$releasever/repo/oss/ mirror-oss 2>/dev/null || true
zypper ar -fcg {base_url}/update/leap/\\$releasever/oss/ mirror-update 2>/dev/null || true
exit 0
'''

    return None


def build_mirror_args(mirror_config: Optional[dict], template_name: str) -> list:
    """
    根据镜像源配置生成 virt-customize 参数。
    返回额外的 virt-customize 参数列表。
    """
    if not mirror_config:
        return []

    os_family = detect_os_family(template_name)
    if not os_family:
        print(f'Warning: Cannot detect OS family for {template_name}, skipping mirror configuration.', file=sys.stderr)
        return []
    if os_family == 'rhel':
        # EL 系各发行版的仓库路径不同（rocky / almalinux / centos-stream 各有镜像目录）：旧版把三家都改成 rhel
        # 的 …/centos 路径，换源后仓库 404、包升级失败。只认按发行版配置的条目；只配了旧的 rhel 不换源（保持官方源）
        variant = detect_rhel_variant(template_name)
        if variant != 'rhel':
            if variant not in mirror_config:
                print(f'Warning: mirror has no "{variant}" entry; keeping the official repositories for {template_name}.',
                      file=sys.stderr)
                return []
            os_family = variant

    mirror_cmd = build_mirror_command(mirror_config, os_family)
    if not mirror_cmd:
        print(f'Warning: Mirror not configured for OS family "{os_family}", skipping.', file=sys.stderr)
        return []

    # 创建临时脚本文件（宿主机侧；--run 会把它上传到镜像内执行，宿主机文件在结束时由 cleanup_temp_files 删除）
    script_fd, script_path = tempfile.mkstemp(prefix='mirror_setup_', suffix='.sh')
    _TEMP_FILES.append(script_path)
    with os.fdopen(script_fd, 'w') as f:
        f.write(mirror_cmd)
    os.chmod(script_path, 0o700)
    return ['--run', script_path]


def http_head(url: str) -> Tuple[Optional[str], Optional[str]]:
    """尝试 HEAD，返回 (ETag, Last-Modified)。若不支持/失败则抛出异常由调用方处理。"""
    head = pitlib.http_head(url)
    return head['etag'], head['last_modified']


def load_meta(meta_path: str) -> dict:
    try:
        with open(meta_path, "r") as f:
            doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_meta(meta_path: str, data: dict):
    pitlib.write_json_atomic(meta_path, data)


def source_unchanged(meta_old: dict, head: dict) -> bool:
    """缓存是否与上游一致：两边都有 ETag 就只认 ETag（ETag 不同但 Last-Modified 相同 = 变了）；否则比 Last-Modified。"""
    if head.get('etag') and meta_old.get('etag'):
        return head['etag'] == meta_old['etag']
    if head.get('last_modified') and meta_old.get('last_modified'):
        return head['last_modified'] == meta_old['last_modified']
    return False


def _progress_bar(total: Optional[int]):
    if tqdm is not None and sys.stderr.isatty():
        return tqdm.tqdm(total=total, unit='B', unit_scale=True, miniters=1)
    return None


def download_file(name: str, url: str, destination: str):
    """下载镜像（带连接/读超时）；仅对临时网络和服务端错误进行有限重试。"""
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        with contextlib.suppress(FileNotFoundError):
            os.remove(destination)
        try:
            with pitlib.http_open(url) as resp, open(destination, 'wb') as out:
                if url.startswith('https://') and not str(resp.geturl()).startswith('https://'):
                    # 镜像分发器（Fedora mirrormanager 等）把 https 重定向到明文 http：完整性只剩同源的校验文件，拒绝
                    raise DownloadError(f'{name}: redirected from https to {resp.geturl()}; refusing a plaintext download', 'tls')
                total = resp.headers.get('Content-Length')
                total = int(total) if total and total.isdigit() else None
                bar = _progress_bar(total)
                done, next_log = 0, 0.1
                if total is not None and total > MAX_IMAGE_BYTES:
                    raise DownloadError(f'{name}: upstream image is {total >> 20} MiB, over the {MAX_IMAGE_BYTES >> 20} MiB limit', 'space')
                for chunk in iter(lambda: resp.read(1 << 20), b''):
                    out.write(chunk)
                    done += len(chunk)
                    if done > MAX_IMAGE_BYTES:
                        raise DownloadError(f'{name}: download exceeded {MAX_IMAGE_BYTES >> 20} MiB; aborted', 'space')
                    if bar is not None:
                        bar.update(len(chunk))
                    elif total and done / total >= next_log:
                        print(f'  {name}: {int(done * 100 / total)}% of {total >> 20} MiB', file=sys.stderr)
                        next_log += 0.1
                if bar is not None:
                    bar.close()
                if total is not None and done != total:
                    raise ConnectionError(f'short read ({done} of {total} bytes)')
            return
        except DownloadError:
            with contextlib.suppress(FileNotFoundError):
                os.remove(destination)
            raise
        except urllib.error.HTTPError as exc:
            retriable = exc.code in RETRIABLE_HTTP_STATUSES
            detail = f'HTTP {exc.code} {exc.reason}'
            final_url = exc.geturl()
            not_found = exc.code == 404
            tls_error = False
        except urllib.error.URLError as exc:
            detail = str(exc.reason)
            final_url = url
            not_found = False
            # 证书问题不是瞬时故障，重试没有意义，直接给出排障提示
            tls_error = (isinstance(exc.reason, ssl.SSLCertVerificationError)
                         or 'CERTIFICATE_VERIFY_FAILED' in detail)
            retriable = not tls_error
        except (OSError, ConnectionError) as exc:
            # 读超时 / 连接被重置 / IncompleteRead：都按临时故障重试
            detail = f'{type(exc).__name__}: {exc}'
            final_url = url
            not_found = tls_error = False
            retriable = True

        if retriable and attempt < DOWNLOAD_RETRIES:
            delay = 2 ** attempt
            print(f'Download failed ({detail}); retrying in {delay}s '
                  f'({attempt}/{DOWNLOAD_RETRIES - 1})...', file=sys.stderr)
            time.sleep(delay)
            continue

        with contextlib.suppress(FileNotFoundError):
            os.remove(destination)
        if not_found:
            hint = 'The configured image URL no longer exists; update templates.yaml with a current URL.'
            kind = 'not-found'
        elif tls_error:
            hint = TLS_HINT
            kind = 'tls'
        else:
            hint = 'Check the URL and the host network/proxy configuration.'
            kind = 'network'
        raise DownloadError(
            f'Failed to download template "{name}": {detail}\n'
            f'URL: {final_url}\n{hint}', kind
        ) from None


def download_any(name: str, urls: list, destination: str) -> str:
    """按顺序试主站与 image_mirrors 备用地址，返回实际下载成功的地址；全部失败抛最后一个（404 优先报）。"""
    errors = []
    for url in urls:
        try:
            download_file(name, url, destination)
            if url != urls[0]:
                print(f'Downloaded {name} from mirror {url}', file=sys.stderr)
            return url
        except DownloadError as e:
            errors.append(e)
            if len(urls) > 1:
                print(f'Warning: {url} failed ({str(e).splitlines()[0]}); trying the next mirror', file=sys.stderr)
    gone = [e for e in errors if e.kind == 'not-found']
    raise (gone[0] if gone and len(gone) == len(errors) else errors[-1])


def verify_download(template: dict, url: str, path: str, config: Optional[dict] = None) -> dict:
    """按模板 checksum 配置核对下载文件；不一致抛 DownloadError，取不到校验文件只告警（记 verified=false）。

    校验文件先从主站取，主站不通再按 image_mirrors 改写去备用站取（与镜像本体同一套规则）。
    """
    filename = url.rsplit('/', 1)[-1]
    try:
        ck_url = pitlib.checksum_url(template, url)
    except (OSError, urllib.error.URLError, pitlib.PitError) as e:
        ck_url, last_error = None, e
    else:
        last_error = None
    if not ck_url:
        if last_error is None and not (template.get('checksum') or template.get('checksum_pattern')):
            return {'verified': False, 'reason': 'no checksum configured'}
        print(f'Warning: checksum for {template["name"]} unavailable ({last_error}); continuing unverified', file=sys.stderr)
        return {'verified': False, 'reason': f'checksum unavailable: {last_error}'[:200]}
    parsed = None
    for candidate in pitlib.mirror_candidates(ck_url, config):
        try:
            parsed = pitlib.parse_checksum(pitlib.http_text(candidate, pitlib.CHECKSUM_MAX_BYTES), filename)
            break
        except (OSError, urllib.error.URLError, pitlib.PitError) as e:
            last_error = e
    if parsed is None and last_error is not None:
        print(f'Warning: checksum for {template["name"]} unavailable ({last_error}); continuing unverified', file=sys.stderr)
        return {'verified': False, 'reason': f'checksum unavailable: {last_error}'[:200]}
    if not parsed or not parsed[0]:
        print(f'Warning: {ck_url} has no entry for {filename}; continuing unverified', file=sys.stderr)
        return {'verified': False, 'reason': 'no entry for file'}
    algo, expected = parsed
    got = pitlib.file_digest(path, algo)
    if got != expected:
        raise DownloadError(f'Checksum mismatch for {template["name"]}: {algo} {got} ≠ {expected} ({ck_url})',
                            'checksum')
    return {'verified': True, 'algo': algo, 'hex': expected}


def upstream_checksum(template: dict, url: str, config: Optional[dict] = None) -> Optional[str]:
    """上游校验文件里这个镜像当前的 hash（取不到返回 None）。sync 用它判断「上游换了没有」：比 ETag 可靠——
    Fedora / openSUSE / Arch 的 HEAD 会被重定向到不同镜像站，ETag 每次都不一样。"""
    filename = url.rsplit('/', 1)[-1]
    try:
        ck_url = pitlib.checksum_url(template, url)
    except (OSError, urllib.error.URLError, pitlib.PitError):
        return None
    if not ck_url:
        return None
    for candidate in pitlib.mirror_candidates(ck_url, config):
        try:
            parsed = pitlib.parse_checksum(pitlib.http_text(candidate, pitlib.CHECKSUM_MAX_BYTES), filename)
        except (OSError, urllib.error.URLError, pitlib.PitError):
            continue
        return parsed[1] if parsed and parsed[0] else None
    return None


def fetch_image(template: dict, refresh: bool = False, config: Optional[dict] = None,
                url: Optional[str] = None, expected_checksum: Optional[str] = None,
                require_checksum: bool = False) -> Tuple[str, dict]:
    """下载（或复用缓存）上游镜像，返回 (img 路径, 来源元数据)。缓存镜像永远是上游原样，不在上面做定制。

    url：调用方已经定好的下载地址（sync 的决策用的那个）；不传才自己解析——两次解析之间索引可能变了。
    """
    name = template['name']
    if config is None:
        config = load_config()
    os.makedirs(CACHE_DIR, exist_ok=True)
    img = os.path.join(CACHE_DIR, f'{name}.img')
    dl = os.path.join(CACHE_DIR, f'{name}.img.download')
    meta_path = os.path.join(CACHE_DIR, f'{name}.meta.json')
    meta_old = load_meta(meta_path)
    if url is None:
        url = pitlib.resolve_source_url(template, log=lambda m: print(m, file=sys.stderr), config=config,
                                        last_known=meta_old.get('url'))

    head = {}
    try:
        head = pitlib.http_head(url)
    except Exception as e:  # noqa: BLE001 - HEAD 只是优化，失败照常下载
        print(f'HEAD {url} failed: {e}', file=sys.stderr)

    # 旧版缓存（没有 url / sha256 字段）被就地定制过，不能复用；sync 已从上游校验文件拿到期望 hash 时，缓存对不上也不能复用
    # （HEAD 失败 / CDN 的 ETag 过期时会拿旧缓存构建，下一轮又判「上游变了」，循环重建）
    cached_hex = (meta_old.get('checksum') or {}).get('hex')
    reusable = (not refresh and os.path.exists(img) and meta_old.get('url') == url and meta_old.get('sha256')
                and (expected_checksum is None or cached_hex == expected_checksum))
    if reusable and (source_unchanged(meta_old, head) or not head):
        why = 'unchanged' if head else 'upstream unreachable, using cache'
        print(f'Image for {name} {why}, skip download.', file=sys.stderr)
        return img, meta_old

    # 下载 + 解包最多同时占两份；没有 Content-Length 时按 4 GiB 估。写完后文件系统至少还要剩 max(10 GiB, 10%)：
    # 不够抛 SpaceError（sync 记 deferred，不算失败、不进退避）
    length = int(head['length']) if str(head.get('length') or '').isdigit() else 2 * pitlib.GIB
    pitlib.ensure_fs_space(f'cache dir {CACHE_DIR}', CACHE_DIR, 2 * length + pitlib.GIB // 2)

    try:
        fetched_from = download_any(name, pitlib.mirror_candidates(url, config), dl)
        checksum = verify_download(template, url, dl, config)
        if require_checksum and not checksum.get('verified') and (template.get('checksum') or template.get('checksum_pattern')):
            # 无人值守构建（sync）：配了校验却核对不了就不建——产物会发给每一台新客户 VM
            raise DownloadError(f'{name}: checksum configured but could not be verified ({checksum.get("reason")}); not building', 'checksum')

        unpack = template.get('unpack')
        with contextlib.suppress(FileNotFoundError):
            os.remove(img)
        if unpack:
            # yaml 里的 unpack 是仓库作者写的固定命令模板；占位符只会是缓存路径（不含空格 / 引号）
            cmd = unpack.replace('{dl}', dl).replace('{img}', img)
            print(f'# {cmd}', file=sys.stderr)
            subprocess.run(cmd, shell=True, check=True)
            with contextlib.suppress(FileNotFoundError):
                os.remove(dl)
        else:
            os.replace(dl, img)
    except BaseException:
        # 失败 / Ctrl-C / SIGTERM：半截的 .img.download 动辄几 GB，不能留在缓存目录里
        with contextlib.suppress(FileNotFoundError):
            os.remove(dl)
        raise

    meta = {
        'url': url,
        'fetched_from': fetched_from,
        'etag': head.get('etag'),
        'last_modified': head.get('last_modified'),
        'sha256': pitlib.file_digest(img, 'sha256'),
        'checksum': checksum,
        'fetched_at': int(time.time()),
    }
    save_meta(meta_path, meta)
    return img, meta


def download_with_cache(name: str, url: str, unpack_cmd: Optional[str] = None, refresh: bool = False) -> str:
    """兼容旧调用方：返回缓存的上游镜像路径。"""
    img, _meta = fetch_image({'name': name, 'url': url, 'unpack': unpack_cmd}, refresh)
    return img


def match_templates(all_tpls: list, filter_expr: Optional[str], families: Optional[list] = None, config: Optional[dict] = None) -> list:
    """支持逗号分隔 / 通配 / 系列名的模板选择（模板名大小写不敏感）。

    - filter_expr 为空时：families 为 None 返回全部，否则只返回属于这些系列的模板。
    - 每个选择项依次尝试：精确名称 → glob 通配 → 系列名（如 rocky / alpine）。
    - @profile 展开成 templates.yaml profiles 里的列表（如 @minimal：小硬盘节点只要几个主流模板）。
    """
    if filter_expr and '@' in filter_expr:
        filter_expr = pitlib.expand_profiles(filter_expr, config if config is not None else load_config())
    if not filter_expr:
        if families is None:
            return list(all_tpls)
        fams = {f.lower() for f in families}
        return [t for t in all_tpls if template_family(t) in fams]
    names = [s.strip() for s in filter_expr.split(",") if s.strip()]
    matched = []
    for n in names:
        if any(ch in n for ch in "*?[]"):
            matched.extend([t for t in all_tpls if fnmatch.fnmatch(t["name"].lower(), n.lower())])
            continue
        exact = [t for t in all_tpls if t["name"].lower() == n.lower()]
        if exact:
            matched.extend(exact)
            continue
        by_family = [t for t in all_tpls if template_family(t) == n.lower()]
        if by_family:
            matched.extend(by_family)
        else:
            print(f'Warning: no template matches "{n}".', file=sys.stderr)
    # 去重保持顺序
    seen = set()
    uniq = []
    for t in matched:
        if t["name"] not in seen:
            seen.add(t["name"])
            uniq.append(t)
    return uniq


# 清理命令的墙钟上限：卡死的 qm stop / destroy（存储挂了、锁等不到）不能让整轮 sync 一直挂着
CLEANUP_TIMEOUT = 600


def quiet_cmd(argv: list, timeout: int = CLEANUP_TIMEOUT) -> bool:
    """尽力而为地跑一条清理命令（不抛异常、带超时）；成功返回 True。"""
    try:
        return subprocess.run([str(a) for a in argv], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=timeout).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def destroy_vm_quietly(vmid: int, storage: Optional[str] = None, volumes_before: Optional[set] = None, log=None):
    """导入中途失败时清理半成品 VM（尽力而为，不抛异常）。

    storage + volumes_before（建之前 `pvesm list` 的快照）都给了，才在 destroy 之后再扫一遍存储，删掉这次才出现的
    vm-<id>-* / base-<id>-* 残留卷（import-from 中途失败、cloudinit 卷没跟着走等）。
    """
    print(f'清理未完成的 VM {vmid} ...', file=sys.stderr)
    # 不带 --destroy-unreferenced-disks：它按 VMID 扫存储上的卷，没组集群却共挂存储的节点会误删别的机器同号 VM 的盘
    quiet_cmd(['qm', 'destroy', vmid, '--purge', '1', '--skiplock', '1'])
    if storage and volumes_before is not None:
        with contextlib.suppress(pitlib.PitError):
            pitlib.free_leftover_volumes(storage, vmid, volumes_before, log=log)


def upgrade_enabled(template: dict, upgrade: bool) -> bool:
    return upgrade and template.get('upgrade', True) is not False


def tuning_enabled(template: dict, apply_tuning: bool) -> bool:
    return apply_tuning and template.get('net_tuning', True) is not False


def recipe_of(template: dict, apply_tuning: bool, upgrade: bool, mirror_name: Optional[str]) -> str:
    """配方指纹：yaml 条目 + 引用到的脚本内容 + 工具版本 + 开关。sync 据此判断「注入方式变了要重建」。"""
    files = customize_files(template.get('customize')) + [VERIFY_SCRIPT, FINALIZE_SCRIPT]
    if upgrade_enabled(template, upgrade):
        files.append(UPGRADE_SCRIPT)
    if tuning_enabled(template, apply_tuning):
        files.append(TUNING_SCRIPT)
    tpl = dict(template)
    tpl['_switches'] = {'tuning': tuning_enabled(template, apply_tuning),
                        'upgrade': upgrade_enabled(template, upgrade), 'mirror': mirror_name or ''}
    tpl.pop('url', None)       # 地址变化由 source 比对负责（discover 会让 url 每个点版本都变）
    tpl.pop('discover', None)
    # 元数据、不影响镜像内容：改个日期 / 显示名 / CPU 要求不能让全机队把这个模板重建一遍
    for key in RECIPE_IGNORED_KEYS:
        tpl.pop(key, None)
    return pitlib.recipe_hash(tpl, files, RECIPE_VERSION)


def customization_args(template: dict, mirror_config: Optional[dict], apply_tuning: bool, upgrade: bool,
                       build_info: str) -> list:
    """virt-customize 参数，顺序有讲究：

    换源 → 包升级（先拿到安全更新，后面的安装走同一份源）→ 模板 customize（ssh / root / qga）→ 网络调优
    → 写构建信息 → verify（qga / sshd 生效配置，不达标整个构建失败，旧模板保持在役）→ finalize（清 host key /
    machine-id / cloud-init 状态 / 日志 / 缓存，必须最后）。
    """
    name = template['name']
    args = build_mirror_args(mirror_config, name)
    if upgrade_enabled(template, upgrade):
        args += ['--run', UPGRADE_SCRIPT]
    args += build_customize_args(template.get('customize'))
    if tuning_enabled(template, apply_tuning):
        args += build_tuning_args()
    args += ['--upload', f'{build_info}:{BUILD_INFO_PATH}', '--chmod', f'0644:{BUILD_INFO_PATH}']
    args += ['--run', VERIFY_SCRIPT, '--run', FINALIZE_SCRIPT]
    if detect_os_family(name) in ('rhel', 'fedora', 'suse'):
        args += ['--selinux-relabel']
    return args


def write_build_info(name: str, built_at: int, source: dict) -> str:
    fd, path = tempfile.mkstemp(prefix='pit_build_info_', suffix='.txt')
    _TEMP_FILES.append(path)
    with os.fdopen(fd, 'w') as fh:
        fh.write(f'TEMPLATE={name}\n')
        fh.write('BUILT_AT=%s\n' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(built_at)))
        fh.write(f'SOURCE_URL={source.get("url", "")}\n')
        fh.write(f'SOURCE_SHA256={source.get("sha256", "")}\n')
        fh.write(f'BUILDER=pve-import-template {TOOL_VERSION}\n')
    return path


# 深度冒烟里的 SSH 检查：不同退出码区分失败项（qga exec 只回退出码与输出）
SMOKE_SSH_SCRIPT = r'''
ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || { echo "no /etc/ssh/ssh_host_*_key"; exit 11; }
SSHD=""
for p in /usr/sbin/sshd /usr/bin/sshd /sbin/sshd; do [ -x "$p" ] && SSHD="$p" && break; done
[ -n "$SSHD" ] || { echo "sshd binary not found"; exit 12; }
mkdir -p /run/sshd 2>/dev/null
"$SSHD" -t 2>&1 || exit 13
if systemctl is-active ssh >/dev/null 2>&1 || systemctl is-active sshd >/dev/null 2>&1 \
   || systemctl is-active ssh.socket >/dev/null 2>&1 || systemctl is-active sshd.socket >/dev/null 2>&1 \
   || rc-service sshd status >/dev/null 2>&1; then
  exit 0
fi
echo "neither ssh nor sshd (service / socket) is active"
exit 14
'''
SMOKE_SSH_FAILURES = {
    11: 'SSH host keys were not generated at first boot',
    12: 'sshd is not installed',
    13: 'sshd -t rejects the configuration',
    14: 'the sshd service is not running',
}
SMOKE_POLL_SECONDS = 10
SMOKE_SSH_ATTEMPTS = 3


def smoke_deep_checks(vmid: int, deadline: float, log, sleep=None) -> list:
    """qga 应答之后、仍在 SMOKE_TIMEOUT 总预算内的加深检查，返回告警列表；不达标抛 PitError（smoke test failed: …）。

    1) cloud-init 跑完：`cloud-init status` 轮询到 done / disabled（done 带 degraded 也算过）；error 失败；超时还在跑也失败。
       来宾里没有 cloud-init 就跳过这一项。
    2) SSH 能用：host key 已生成、`sshd -t` 通过、sshd（服务或 socket）在跑。
    qga 不支持 exec（旧 agent / 禁用了 guest-exec）：两项都跳过、记告警，不失败。
    """
    sleep = sleep or time.sleep
    warnings = []
    try:
        while True:
            res = pitlib.guest_exec(vmid, ['cloud-init', 'status'], timeout=30)
            text = (res['out'] + '\n' + res['err']).strip()
            m = re.search(r'^\s*status:\s*([a-z][a-z ]*?)\s*$', text, re.M | re.I)
            status = m.group(1).lower() if m else ''
            if status in ('done', 'disabled'):
                log(f'smoke test: cloud-init status: {status}')
                break
            if status == 'error':
                raise pitlib.PitError('smoke test failed: cloud-init finished with an error: %s'
                                      % ' '.join(text.split())[:300])
            if time.time() >= deadline:
                raise pitlib.PitError('smoke test failed: cloud-init still %s when the smoke-test time ran out'
                                      % (status or 'not finished'))
            sleep(SMOKE_POLL_SECONDS)
    except pitlib.GuestCommandMissing:
        log('smoke test: cloud-init is not installed in the guest; skipped the cloud-init check')
    except pitlib.GuestExecUnsupported as e:
        warnings.append('guest agent cannot exec commands; skipped the deep smoke checks (%s)' % str(e)[:120])
        log('Warning: ' + warnings[-1])
        return warnings

    last = None
    for attempt in range(SMOKE_SSH_ATTEMPTS):
        try:
            res = pitlib.guest_exec(vmid, ['sh', '-c', SMOKE_SSH_SCRIPT], timeout=30)
        except pitlib.GuestExecUnsupported as e:
            warnings.append('guest agent cannot exec commands; skipped the SSH smoke check (%s)' % str(e)[:120])
            log('Warning: ' + warnings[-1])
            return warnings
        if res['exited'] and res['exitcode'] == 0:
            log('smoke test: SSH host keys present, sshd -t ok, sshd running')
            return warnings
        output = ' '.join((res['out'] + ' ' + res['err']).split())[:300]
        if not res['exited']:
            last = 'the SSH check did not finish within 30s'
        else:
            last = '%s (exit %s): %s' % (SMOKE_SSH_FAILURES.get(res['exitcode'], 'SSH check failed'),
                                        res['exitcode'], output or '-')
        # sshd 可能比 cloud-init 晚几秒起来：重试几次再判失败
        if attempt < SMOKE_SSH_ATTEMPTS - 1 and time.time() + SMOKE_POLL_SECONDS < deadline:
            sleep(SMOKE_POLL_SECONDS)
            continue
        break
    raise pitlib.PitError('smoke test failed: ' + last)


def smoke_test(template_vmid: int, allocate, timeout: int, log, memory: int = 512, deep: bool = False,
               storage: Optional[str] = None, sleep=None) -> list:
    """切换前的开机闸：从新模板克隆一台临时 VM（拔掉所有网卡、摘掉 hookscript），开机后 timeout 秒内 qemu-guest-agent
    必须应答（deep=True 时还要 cloud-init 跑完、SSH 可用，见 smoke_deep_checks），然后销毁。包升级会在 chroot 里动
    内核 / initramfs / grub / extlinux；verify-image.sh 只能看文件，看不出「还能不能开机」。返回告警列表。

    allocate：每次调用给一个临时号（从区间顶端往下取，不推高模板的游标）。撞号（集群里别的节点刚占了）换号重试，
    链接克隆不支持时退回的全克隆那一步也一样。
    storage：模板所在存储；给了就在销毁后扫一遍残留卷（只删这次出现的 vm-<tmp>-*）。
    """
    sleep = sleep or time.sleep
    tmp = None
    created = False
    before = None
    name = f'pitsmoke-{template_vmid}'
    try:
        for attempt in range(3):
            tmp = allocate()
            before = pitlib.storage_volumes(storage, tmp) if storage else None
            try:
                pitlib.run_cmd(['qm', 'clone', template_vmid, tmp, '--name', name, '--full', '0'], timeout=600, log=log)
            except pitlib.CommandError as e:
                if pitlib.COLLISION_RE.search(e.output) and attempt < 2:
                    continue  # 集群里别的节点刚占了这个号
                if 'linked clone' not in e.output.lower():
                    raise
                # 只有「存储不支持链接克隆」（厚置备 LVM 等）才退回全克隆；锁超时之类的错误不该变成一次整盘拷贝
                try:
                    pitlib.run_cmd(['qm', 'clone', template_vmid, tmp, '--name', name, '--full', '1'],
                                   timeout=1800, log=log)
                except pitlib.CommandError as e2:
                    if pitlib.COLLISION_RE.search(e2.output) and attempt < 2:
                        continue
                    raise
            created = True
            break
        cfg = pitlib.vm_config(pitlib.local_node_name() or '', tmp)
        drop = [k for k in cfg if re.fullmatch(r'net\d+', k)] + (['hookscript'] if cfg.get('hookscript') else [])
        args = ['qm', 'set', tmp, '--tags', pitlib.TAG_SMOKE, '--onboot', '0', '--memory', max(int(memory), 1024)]
        if drop:
            # 不接网（不会 DHCP、不碰客户网络）；不跑 hook（hook 会注入路由 / 上报事件）
            args += ['--delete', ','.join(drop)]
        pitlib.run_cmd(args, timeout=120, log=log)
        pitlib.run_cmd(['qm', 'start', tmp], timeout=180, log=log)
        deadline = time.time() + timeout
        while True:
            try:
                pitlib.run_cmd(['qm', 'guest', 'cmd', tmp, 'ping'], timeout=20)
                log(f'smoke test: VM {tmp} booted and the guest agent answered')
                break
            except pitlib.CommandError:
                if time.time() >= deadline:
                    raise pitlib.PitError(f'smoke test failed: a VM cloned from the new template did not answer the guest agent '
                                          f'within {timeout}s (bootloader / kernel broken by the upgrade?)') from None
                sleep(SMOKE_POLL_SECONDS)
        return smoke_deep_checks(tmp, deadline, log, sleep=sleep) if deep else []
    finally:
        if created and tmp is not None:
            quiet_cmd(['qm', 'stop', tmp, '--skiplock', '1', '--timeout', '30'], timeout=180)
            quiet_cmd(['qm', 'destroy', tmp, '--purge', '1', '--skiplock', '1'])
            if storage:
                with contextlib.suppress(pitlib.PitError):
                    pitlib.free_leftover_volumes(storage, tmp, before, log=log)


def build_template(template: dict, storage_name: str, vmid, *, vm_name: str,
                   mirror_config: Optional[dict] = None, mirror_name: Optional[str] = None,
                   apply_tuning: bool = True, upgrade: bool = True, refresh: bool = False,
                   vm_opts: Optional[dict] = None, force_tcg: bool = False, keep_cache: bool = True,
                   node: Optional[str] = None, log=None, source_url: Optional[str] = None,
                   smoke_timeout: Optional[int] = None, smoke_allocate=None, extra_config: Optional[dict] = None,
                   disk_format: Optional[str] = None, expected_checksum: Optional[str] = None,
                   require_checksum: bool = False, smoke_deep: bool = False, min_free_mem_mb: int = 0) -> dict:
    """下载 → 在副本上定制 → 建 VM（import-from）→ qm template →（可选）开机冒烟。返回写进模板的元数据（含 vmid）。

    vmid：整数，或无参可调用对象（每次调用重新分配一个号）——传可调用对象时 VMID 在 qm create 前一刻才分配，
    撞上 already exists / File exists（集群里别的节点同时在建、存储上有同号残留卷）换号重试；存储上已有这个号的卷时
    干脆不用这个号。
    任何一步失败都清理半成品 VM（及这次才出现的残留卷）与副本并抛异常；成功返回后模板已是 template=1，名字是 vm_name。
    空间不够（写入后低于保留量）/ 冒烟前内存不够抛 pitlib.Deferred（调用方记推迟，不算失败）。
    keep_cache=False：结束后（成功或失败）删掉缓存的上游镜像，只留元数据——小硬盘节点不常驻十几 GB 缓存。
    smoke_timeout：秒；None = 不做开机冒烟。smoke_allocate：冒烟临时 VM 的号（默认同 vmid 分配器）。
    smoke_deep：冒烟时再查 cloud-init 跑完、SSH 可用（见 smoke_deep_checks）。min_free_mem_mb：冒烟前的可用内存下限。
    extra_config：沿用旧模板的节点策略键（hookscript / vga / machine / … 见 pitlib.CARRY_OVER_KEYS）。
    disk_format：系统盘格式（文件类存储传 qcow2；None = 存储默认）。
    expected_checksum / require_checksum：见 fetch_image。
    返回的元数据里 carried = 实际沿用的键，warnings = 冒烟告警（如 qga 不支持 exec 跳过了深度检查）。
    """
    log = log or (lambda m: print(m, file=sys.stderr))
    name = template['name']
    if not TEMPLATE_NAME_RE.match(name):
        raise pitlib.PitError(f'template name {name!r} is not a valid VM name')
    opts = dict(VM_DEFAULTS)
    opts.update(vm_opts or {})
    img_cache = os.path.join(CACHE_DIR, f'{name}.img')
    node = node or pitlib.local_node_name() or ''

    fixed_vmid = vmid
    allocate = vmid if callable(vmid) else (lambda: fixed_vmid)  # 不能直接 lambda: vmid——下面会把 vmid 置 None
    attempts = 5 if callable(vmid) else 1   # 固定号撞了换不了号，重试没有意义
    carried = pitlib.carry_over(extra_config or {})
    # ostype / scsihw 是 qm create / import 本来就要设的键：沿用旧值替换默认值，不重复传参
    ostype = carried.get('ostype', 'l26')
    scsihw = carried.get('scsihw', 'virtio-scsi-single')
    extra = {k: v for k, v in carried.items() if k not in ('ostype', 'scsihw')}
    created = False
    work = None
    vmid = None
    volumes_before = None
    try:
        # 目标存储写之前就已低于保留量（或 thin pool 元数据快满）：不下载，直接推迟
        pitlib.ensure_storage_space(node, storage_name, 0)
        img, source = fetch_image(template, refresh, url=source_url, expected_checksum=expected_checksum,
                                  require_checksum=require_checksum)
        fmt = pitlib.inspect_image(img)
        virtual, actual = pitlib.image_sizes(img)
        built_at = int(time.time())
        work_dir = os.path.join(CACHE_DIR, 'work')
        os.makedirs(work_dir, exist_ok=True)
        work = os.path.join(work_dir, f'{name}.{os.getpid()}.img')
        # 副本 + 包升级的增量；目标存储按虚拟大小估（厚置备 LVM 会整块分配，精简置备只会更少）。写入后两边都还要留余量
        pitlib.ensure_fs_space(f'cache dir {CACHE_DIR}', work_dir, actual + pitlib.GIB)
        pitlib.ensure_storage_space(node, storage_name, virtual + pitlib.GIB // 2)

        meta = {
            'v': pitlib.META_VERSION,
            'name': name,
            'tool': TOOL_VERSION,
            'built_at': built_at,
            'source': {**{k: source.get(k) for k in ('url', 'etag', 'last_modified', 'sha256')},
                       'checksum': (source.get('checksum') or {}).get('hex')},
            'checksum_verified': bool((source.get('checksum') or {}).get('verified')),
            'upgraded': upgrade_enabled(template, upgrade),
            'tuning': tuning_enabled(template, apply_tuning),
            'recipe': recipe_of(template, apply_tuning, upgrade, mirror_name),
        }

        info = write_build_info(name, built_at, source)
        pitlib.run_virt_customize(work, customization_args(template, mirror_config, apply_tuning, upgrade, info),
                                  timeout=CUSTOMIZE_TIMEOUT, force_tcg=force_tcg, log=log, fmt=fmt,
                                  prepare=lambda: shutil.copyfile(img, work))

        tags = pitlib.format_tags(pitlib.managed_tags(built_at))
        # 定制写的是来宾磁盘本身：raw 镜像的第 0 扇区被改写成带 backing 的 qcow2 头，扁平化导入时就会把宿主机文件带进模板
        if pitlib.inspect_image(work) != fmt:
            raise pitlib.PitError('customized image changed format; refusing to import it')
        extra_args = [x for key, value in sorted(extra.items()) for x in ('--' + key, value)]
        fmt_opt = f',format={disk_format}' if disk_format else ''
        for attempt in range(attempts):
            last = attempt == attempts - 1
            vmid = allocate()
            volumes_before = pitlib.storage_volumes(storage_name, vmid)
            if volumes_before and not last:
                # 存储上已有这个号的卷（共挂存储的另一台机器 / 早先的残留）：import-from / cloudinit 卷会撞 File exists
                log(f'storage {storage_name} already has volumes for VMID {vmid} '
                    f'({", ".join(sorted(volumes_before)[:3])}); trying another')
                continue
            try:
                pitlib.run_cmd(['qm', 'create', vmid, '--name', vm_name,
                                '--memory', opts['memory'], '--cores', opts['cores'],
                                '--net0', f'virtio,bridge={opts["bridge"]},queues=4',
                                '--cpu', opts['cpu'], '--ostype', ostype,
                                '--agent', 'enabled=1,fstrim_cloned_disks=1',
                                '--description', pitlib.encode_description(meta),
                                '--tags', tags, *extra_args], timeout=300, log=log)
            except pitlib.CommandError as e:
                # 构建要跑很久：期间集群里别的节点可能占了这个号。别人的 VM 绝不碰（created 还是 False），换号再来
                if not pitlib.COLLISION_RE.search(e.output) or last:
                    raise
                log(f'VMID {vmid} was taken while building; trying another')
                continue
            created = True
            try:
                # import-from 一步导入并挂载，不再猜卷名（旧版写死 disk-0，存储上有残留盘时会挂错盘）
                pitlib.run_cmd(['qm', 'set', vmid, '--scsihw', scsihw,
                                '--scsi0', f'{storage_name}:0,import-from={work},discard=on{fmt_opt}'], timeout=1800, log=log)
                pitlib.run_cmd(['qm', 'set', vmid, '--boot', 'order=scsi0', '--serial0', 'socket'], timeout=300, log=log)
                if template.get('cloud_init'):
                    pitlib.run_cmd(['qm', 'set', vmid, '--ide2', f'{storage_name}:cloudinit',
                                    '--ciuser', 'root', '--ipconfig0', 'ip=dhcp'], timeout=300, log=log)
            except pitlib.CommandError as e:
                if not pitlib.COLLISION_RE.search(e.output) or last:
                    raise
                # 分配卷时撞上同号卷（vm-<id>-cloudinit 之类）：删掉自己这台、换号重来
                log(f'VMID {vmid}: a volume with this number already exists on {storage_name}; trying another')
                destroy_vm_quietly(vmid, storage_name, volumes_before, log)
                created = False
                continue
            break
        meta['vmid'] = vmid
        meta['carried'] = sorted(carried)
        pitlib.run_cmd(['qm', 'template', vmid], timeout=600, log=log)
        if smoke_timeout:
            # 冒烟 VM 要 1 GiB：宿主机已经吃紧就别再开一台（建好的模板随失败清理删掉，下一轮再来）
            pitlib.ensure_memory(min_free_mem_mb)
            warnings = smoke_test(vmid, smoke_allocate or allocate, smoke_timeout, log, memory=int(opts['memory']),
                                  deep=smoke_deep, storage=storage_name)
            if warnings:
                meta['warnings'] = warnings
    except BaseException:
        if created:
            destroy_vm_quietly(vmid, storage_name, volumes_before, log)
        raise
    finally:
        if work:
            with contextlib.suppress(FileNotFoundError):
                os.remove(work)
        if not keep_cache:
            with contextlib.suppress(FileNotFoundError):
                os.remove(img_cache)
        cleanup_temp_files()
    return meta


def import_template(template: dict, storage: StorageInfo.Base, vmid: int, keep_image: bool = True,
                    refresh: bool = False, mirror_config: Optional[dict] = None, apply_tuning: bool = True,
                    vm_opts: Optional[dict] = None, mirror_name: Optional[str] = None,
                    upgrade: bool = True) -> str:
    """导入单个模板；返回 'ok' 或 'skipped'，失败抛异常（半成品 VM 会被清理）。"""
    name = template['name']
    if vm_exists_by_name(name):
        print(f'VM with name "{name}" exists, skipping (use sync-templates.py to rebuild it).')
        return 'skipped'

    print(f'Importing VMID {vmid} ({name})')
    build_template(template, storage.name, vmid, vm_name=name, mirror_config=mirror_config,
                   mirror_name=mirror_name, apply_tuning=apply_tuning, upgrade=upgrade, refresh=refresh,
                   vm_opts=vm_opts, log=print, disk_format='qcow2' if isinstance(storage, StorageInfo.Dir) else None,
                   extra_config=node_template_defaults())

    if not keep_image:
        img = os.path.join(CACHE_DIR, f'{name}.img')
        print(f'Deleting {img}')
        with contextlib.suppress(FileNotFoundError):
            os.remove(img)

    print('Done\n')
    return 'ok'


def node_template_defaults() -> dict:
    """新导入的模板沿用本节点已有模板最常见的 hookscript（deploy-hook.sh --attach 9000-9999 挂在模板上，客户 VM 克隆继承）。"""
    node = pitlib.local_node_name()
    if not node:
        return {}
    counts = {}
    with contextlib.suppress(pitlib.PitError, ValueError):
        for vm in pitlib.node_vms(node):
            if vm['template'] and 9001 <= vm['vmid'] <= 9999:
                hook = pitlib.carry_over(pitlib.vm_config(node, vm['vmid'])).get('hookscript')
                if hook:
                    counts[hook] = counts.get(hook, 0) + 1
    return {'hookscript': max(counts, key=counts.get)} if counts else {}


def print_summary(results: dict):
    total = sum(len(v) for v in results.values())
    print(f'\n===== 导入汇总：{total} 个模板 =====')
    if results['ok']:
        print(f'  成功 {len(results["ok"])}：' + ', '.join(f'{n}({v})' for n, v in results['ok']))
    if results['skipped']:
        print(f'  跳过 {len(results["skipped"])}：' + ', '.join(n for n, _ in results['skipped']))
    if results['failed']:
        print(f'  失败 {len(results["failed"])}：')
        for name, reason in results['failed']:
            print(f'    - {name}: {reason}')
        print('  失败的模板可修复原因后用 --only-new 重新运行，已成功的会自动跳过。')


USAGE = '''Usage: python3 import.py <storage-name> <start-vmid> [template-name[,name2|glob|family]] [options]

不指定模板时导入 templates.yaml 的默认配置组（default_profile，默认 @standard：在售发行版的受支持版本，
不含 EOL）；--all 导入全部。选择器可写模板名、glob 通配、系列名或 @profile（小硬盘节点用 @minimal）。
已存在的模板要换成新构建，请用 sync-templates.py。

Options:
  --all               导入 templates.yaml 中的全部模板（含 EOL，忽略 default_profile）
  --only-new          只导入本节点尚不存在的模板
  --refresh           忽略缓存，强制重新下载镜像
  --mirror <name>     使用 templates.yaml 中配置的镜像源（内网环境）
  --no-tuning         本次导入不注入网络/内核 sysctl 调优（默认开启）
  --no-upgrade        不在镜像内执行包升级（默认升级到最新安全补丁）
  --insecure          下载镜像时跳过 TLS 证书校验（仅用于临时绕过证书问题）
  --bridge <name>     模板网卡所接网桥（默认 vmbr0）
  --memory <MB>       模板内存大小（默认 512）
  --cores <n>         模板 CPU 核数（默认 1）
  --dry-run           只打印导入计划（模板与分配的 VMID），不下载不创建
  --list              列出 templates.yaml 中所有可用模板后退出
  --tui               启动交互式向导（等价于 python3 tui.py）
  -h, --help          显示本帮助

Examples:
  python3 import.py local-lvm 9001
  python3 import.py local-lvm 9001 --all
  python3 import.py local-lvm 9001 --only-new
  python3 import.py local-lvm 9001 @minimal
  python3 import.py local-lvm 9001 'ubuntu-*',rocky --mirror tsinghua
  python3 import.py local-lvm 9001 --bridge vmbr1 --memory 1024 --cores 2
  python3 import.py --list'''


_CONFIG_CACHE: Optional[dict] = None


def load_config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg_path = os.path.join(SCRIPT_DIR, 'templates.yaml')
    with open(cfg_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    for tpl in config.get('templates', []) or []:
        if not TEMPLATE_NAME_RE.match(str(tpl.get('name', ''))):
            raise pitlib.PitError(f'templates.yaml: invalid template name {tpl.get("name")!r}')
    _CONFIG_CACHE = config
    return config


def print_template_list():
    config = load_config()
    _, existing = cluster_vm_index()
    existing_lower = {n.lower() for n in existing}
    defaults = set(default_template_names(config))
    groups = {}
    for t in config.get('templates', []):
        groups.setdefault(template_family(t), []).append(t)
    profile = default_profile(config)
    print(f'可用模板（templates.yaml）：  * = 默认配置组 @{profile}（不指定模板时导入）')
    for fam, tpls in groups.items():
        print(f'\n  {family_label(fam)} ({fam})')
        for t in tpls:
            name = t['name']
            mark = '*' if name in defaults else ' '
            flags = []
            if pitlib.is_eol(t):
                flags.append('EOL')
            elif isinstance(t.get('eol'), str):
                flags.append(f'支持到 {t["eol"]}')
            if name.lower() in existing_lower:
                flags.append('已存在')
            if t.get('net_tuning', True) is False:
                flags.append('调优:关')
            if t.get('upgrade', True) is False:
                flags.append('升级:关')
            if t.get('discover'):
                flags.append('自动找最新版')
            if t.get('checksum'):
                flags.append('校验')
            suffix = f'  [{", ".join(flags)}]' if flags else ''
            print(f'  {mark} - {name}{suffix}')
    others = ', '.join('@' + p for p in (config.get('profiles') or {}) if p != profile)
    print(f'\n默认配置组 @{profile}：{len(defaults)} 个（--all 导入全部；其他配置组：{others or "无"}；选择器也可写系列名）')
    mirrors = config.get('mirrors', {})
    if mirrors:
        print(f'可用镜像源：{", ".join(mirrors.keys())}')


def main():
    # 提前处理无需依赖/参数的子命令
    if any(a in ('-h', '--help') for a in sys.argv[1:]):
        print(USAGE)
        return
    if '--list' in sys.argv[1:]:
        print_template_list()
        return
    if '--tui' in sys.argv[1:]:
        tui = os.path.join(SCRIPT_DIR, 'tui.py')
        rest = [a for a in sys.argv[1:] if a != '--tui']
        os.execv(sys.executable, [sys.executable, tui, *rest])

    # 依赖检查（只验证存在，不强制版本）
    for tool in ('virt-customize', 'pvesh', 'qm'):
        if shutil.which(tool) is None:
            print(f'Missing dependency: {tool}. Please ensure virt-customize / pvesh / qm are available (run setup.sh).')
            sys.exit(2)

    if len(sys.argv) < 3:
        print(USAGE)
        sys.exit(1)

    storage_name = sys.argv[1]
    try:
        start_vmid = int(sys.argv[2])
    except ValueError:
        print('Error: <start-vmid> must be an integer.')
        sys.exit(1)

    # 解析可选参数
    filters = []
    only_new = False
    refresh = False
    apply_tuning = True
    upgrade = True
    insecure = False
    import_all = False
    dry_run = False
    mirror_name = None
    vm_opts = {}
    args = sys.argv[3:]
    i = 0

    def take_value(opt: str) -> str:
        nonlocal i
        if i + 1 >= len(args):
            print(f'Error: {opt} requires a value.')
            sys.exit(1)
        i += 1
        return args[i]

    while i < len(args):
        arg = args[i]
        if arg == '--only-new':
            only_new = True
        elif arg == '--refresh':
            refresh = True
        elif arg == '--no-tuning':
            apply_tuning = False
        elif arg == '--no-upgrade':
            upgrade = False
        elif arg == '--insecure':
            insecure = True
        elif arg == '--all':
            import_all = True
        elif arg == '--dry-run':
            dry_run = True
        elif arg == '--mirror':
            mirror_name = take_value(arg)
        elif arg == '--bridge':
            vm_opts['bridge'] = take_value(arg)
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,32}', vm_opts['bridge']):
                print('Error: --bridge has an invalid value.')
                sys.exit(1)
        elif arg in ('--memory', '--cores'):
            value = take_value(arg)
            if not value.isdigit() or int(value) <= 0:
                print(f'Error: {arg} must be a positive integer.')
                sys.exit(1)
            vm_opts[arg[2:]] = int(value)
        elif not arg.startswith('-'):
            filters.append(arg)   # 多个选择器合并（旧版只保留最后一个）
        else:
            print(f'Error: unknown option "{arg}".')
            print(USAGE)
            sys.exit(1)
        i += 1
    template_filter = ','.join(filters) or None

    if insecure:
        disable_tls_verification()

    storage = check_storage(storage_name)

    config = load_config()
    all_tpls = config['templates']
    mirrors = config.get('mirrors', {})

    # 获取镜像源配置
    mirror_config = None
    if mirror_name:
        if mirror_name not in mirrors:
            print(f'Error: Mirror "{mirror_name}" not found in templates.yaml.')
            print(f'Available mirrors: {", ".join(mirrors.keys()) if mirrors else "(none)"}')
            sys.exit(1)
        mirror_config = mirrors[mirror_name]
        print(f'Using mirror: {mirror_name}')

    if not (import_all or template_filter):
        template_filter = ','.join(default_template_names(config))
        print(f'未指定模板：导入默认配置组 @{default_profile(config)}（{template_filter}；--all 导入全部，@minimal 给小硬盘）。')
    to_import_all = match_templates(all_tpls, template_filter)
    if not to_import_all:
        print('No templates matched. Use --list to see available templates.')
        sys.exit(1)

    # VMID 跨集群判断占用（全局唯一）；「是否已存在」只看本节点（每个节点用本地存储时各有一份同名模板）
    used_vmids, _cluster_names = cluster_vm_index()
    existing_names = list_existing_vm_names()
    if only_new:
        skipped = [t['name'] for t in to_import_all if t['name'] in existing_names]
        if skipped:
            print(f'--only-new：跳过已存在的模板 {", ".join(skipped)}')
        to_import_all = [t for t in to_import_all if t['name'] not in existing_names]
        if not to_import_all:
            print('所有匹配的模板都已存在，无需导入。')
            return

    if dry_run:
        print('\n导入计划（--dry-run，不会下载或创建）：')
        planned = start_vmid
        for tpl in to_import_all:
            if tpl['name'] in existing_names:
                print(f'  {"-":>6}  {tpl["name"]}  (已存在，将跳过)')
                continue
            while planned in used_vmids:
                planned += 1
            print(f'  {planned:>6}  {tpl["name"]}')
            used_vmids.add(planned)
            planned += 1
        opts = dict(VM_DEFAULTS)
        opts.update(vm_opts)
        print(f'\nstorage={storage.name} bridge={opts["bridge"]} memory={opts["memory"]} cores={opts["cores"]} '
              f'tuning={"on" if apply_tuning else "off"} upgrade={"on" if upgrade else "off"} mirror={mirror_name or "-"}')
        return

    print(f'镜像缓存目录：{CACHE_DIR}')
    vmid = start_vmid
    results = {'ok': [], 'skipped': [], 'failed': []}
    with pitlib.ToolLock():
        for tpl in to_import_all:
            while vmid in used_vmids:
                print(f'VMID {vmid} 已被占用（集群内），跳过。')
                vmid += 1
            try:
                status = import_template(
                    tpl,
                    storage,
                    vmid,
                    keep_image=not refresh,
                    refresh=refresh,
                    mirror_config=mirror_config,
                    apply_tuning=apply_tuning,
                    vm_opts=vm_opts,
                    mirror_name=mirror_name,
                    upgrade=upgrade,
                )
            except (DownloadError, pitlib.PitError, subprocess.CalledProcessError, OSError) as exc:
                reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                print(f'\nError: 导入 {tpl["name"]} 失败：{reason}\n', file=sys.stderr)
                results['failed'].append((tpl['name'], reason))
                # 失败的号可能被别人占着（并发创建 / 非本工具的 VM）：跳过它，别让后续模板在同一个号上连环失败
                used_vmids.add(vmid)
                vmid += 1
                continue
            if status == 'skipped':
                results['skipped'].append((tpl['name'], vmid))
                continue
            results['ok'].append((tpl['name'], vmid))
            used_vmids.add(vmid)
            vmid += 1

    print_summary(results)
    if results['failed']:
        sys.exit(1)


if __name__ == '__main__':
    try:
        pitlib.raise_on_sigterm()
        main()
    except pitlib.LockBusy as exc:
        print(f'\nError: {exc}', file=sys.stderr)
        sys.exit(3)
    except (DownloadError, pitlib.PitError) as exc:
        print(f'\nError: {exc}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\n已中断。', file=sys.stderr)
        sys.exit(130)
