#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pitlib.py —— import.py / sync-templates.py / update-templates.py 共用的底层能力。

只放「与交互无关、可单测」的部分：
- 全局锁（同一节点上三个工具互斥，避免并发写同一缓存 / 抢同一 VMID）
- PVE 查询（本节点名、集群 VMID 占用、本节点模板列表、VM 配置、系统盘）
- 模板元数据（tags + description 里的 pit-meta JSON 行）
- VMID 分配、retired 命名、链接克隆引用扫描
- 上游最新版发现（目录索引 + 正则）、校验和解析与校验
- virt-customize 执行（只有 appliance 起不来才回退 TCG，脚本本身出错不重跑）
- 严格的 KEY=VALUE 配置文件解析（不经 shell）

所有外部命令一律 list argv，不拼 shell。
"""

import contextlib
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(SCRIPT_DIR, 'uploads')

LOCK_FILE = os.environ.get('PIT_LOCK_FILE', '/run/lock/pve-import-template.lock')
STATE_DIR = os.environ.get('PIT_STATE_DIR', '/var/lib/pve-import-template')
LOG_DIR = os.environ.get('PIT_LOG_DIR', '/var/log/pve-import-template')
PVE_NODES_DIR = os.environ.get('PIT_PVE_NODES_DIR', '/etc/pve/nodes')

HTTP_TIMEOUT = 60
INDEX_MAX_BYTES = 8 * 1024 * 1024
CHECKSUM_MAX_BYTES = 2 * 1024 * 1024

# 打在托管模板上的 tag：pit = 本工具构建；pit-bYYYYMMDD = 构建日期（平台从 /nodes/<n>/qemu 列表直接读，不必逐台读配置）；
# pit-retired = 已被新构建替换、等待回收（平台不再把它列给客户）
TAG_MANAGED = 'pit'
TAG_RETIRED = 'pit-retired'
TAG_BUILD_PREFIX = 'pit-b'
# 开机冒烟用的临时 VM（构建结束即销毁；被强杀留下的由下一轮 sync 清掉）
TAG_SMOKE = 'pit-smoke'
META_PREFIX = 'pit-meta:'
META_VERSION = 1
RETIRED_INFIX = '-retired-'

# virt-customize 输出中出现这些关键字，说明是 libguestfs appliance 起不来（宿主机环境问题），
# 而不是某个模板 / 脚本本身的问题
LAUNCH_FAILURE_MARKERS = ('guestfs_launch', 'appliance', 'supermin', 'could not access kvm', '/dev/kvm')

# qm config 中代表磁盘的前缀；EFI / TPM / cloudinit / cdrom 会被跳过
DISK_PREFIXES = ('scsi', 'virtio', 'sata', 'ide')


class PitError(RuntimeError):
    """可预期的失败（给人看的一句话），不打 traceback。"""


class LockBusy(PitError):
    pass


class LaunchError(PitError):
    """libguestfs appliance 启动失败（KVM / TCG）。"""

    def __init__(self, message: str, output: str = ''):
        super().__init__(message)
        self.output = output


class CommandError(PitError):
    def __init__(self, message: str, returncode: int = 1, output: str = ''):
        super().__init__(message)
        self.returncode = returncode
        self.output = output


# ---------------------------------------------------------------- 锁 / 信号

class ToolLock:
    """节点级互斥锁（flock，进程退出自动释放，不会因强杀残留）。"""

    def __init__(self, path: str = None):
        self.path = path or LOCK_FILE
        self.fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(self.fd)
            self.fd = None
            if e.errno in (errno.EAGAIN, errno.EACCES):
                raise LockBusy('another pve-import-template run holds %s' % self.path) from None
            raise
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            self.fd = None
        return False


def raise_on_sigterm():
    """SIGTERM 当 Ctrl-C 处理：timer / agent 超时杀进程时也走 finally 清理半成品 VM 与临时文件。"""
    def handler(signum, _frame):
        raise KeyboardInterrupt('signal %d' % signum)
    signal.signal(signal.SIGTERM, handler)


# ---------------------------------------------------------------- 外部命令

def run_cmd(argv: list, timeout: Optional[int] = None, env: Optional[dict] = None, log=None) -> str:
    """执行命令，返回合并输出；失败抛 CommandError。永远 list argv；超时杀整个进程组。"""
    if log:
        log('# ' + ' '.join(str(a) for a in argv))
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.Popen([str(a) for a in argv], env=full_env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        raise CommandError('cannot execute %s: %s' % (argv[0], e), 127) from None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        raise CommandError('%s timed out after %ss' % (argv[0], timeout), 124,
                           (out or b'').decode('utf-8', 'replace')) from None
    except KeyboardInterrupt:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    text = (out or b'').decode('utf-8', 'replace')
    if log and text.strip():
        log(text.rstrip())
    if proc.returncode != 0:
        last = next((l for l in reversed(text.strip().splitlines()) if l.strip()), '')
        raise CommandError('%s failed (exit %d): %s' % (argv[0], proc.returncode, last[:300]),
                           proc.returncode, text)
    return text


def pvesh_json(path: str, *args) -> object:
    out = run_cmd(['pvesh', 'get', path, *args, '--output-format', 'json'], timeout=120)
    return json.loads(out or 'null')


# ---------------------------------------------------------------- PVE 查询

def local_node_name() -> Optional[str]:
    """本节点名：/etc/pve/local 是指向 /etc/pve/nodes/<nodename> 的符号链接；回退短主机名。"""
    try:
        name = os.path.basename(os.readlink('/etc/pve/local').rstrip('/'))
        if name:
            return name
    except OSError:
        pass
    try:
        return os.uname().nodename.split('.')[0] or None
    except Exception:
        return None


def cluster_used_vmids() -> set:
    """集群内已占用的 VMID（qemu + lxc：VMID 跨类型全局唯一，漏掉 lxc 会让 qm create 撞号）。"""
    used = set()
    with contextlib.suppress(PitError, ValueError, TypeError):
        for r in pvesh_json('/cluster/resources', '--type', 'vm') or []:
            if r.get('vmid') is not None:
                used.add(int(r['vmid']))
    # 单机或 pvesh 失败：直接看 pmxcfs 里的配置文件
    for pattern_dir in ('qemu-server', 'lxc'):
        with contextlib.suppress(OSError):
            for node in os.listdir(PVE_NODES_DIR):
                d = os.path.join(PVE_NODES_DIR, node, pattern_dir)
                with contextlib.suppress(OSError):
                    for f in os.listdir(d):
                        m = re.match(r'^(\d+)\.conf$', f)
                        if m:
                            used.add(int(m.group(1)))
    return used


def parse_tags(raw) -> list:
    if not raw:
        return []
    return [t for t in re.split(r'[;,\s]+', str(raw).strip().lower()) if t]


def format_tags(tags) -> str:
    seen, out = set(), []
    for t in tags:
        t = str(t).strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return ';'.join(out)


def node_vms(node: str) -> list:
    """本节点 qemu VM：[{vmid, name, template(bool), status, tags[list], lock}]。"""
    out = []
    for r in pvesh_json('/nodes/%s/qemu' % node) or []:
        try:
            vmid = int(r.get('vmid'))
        except (TypeError, ValueError):
            continue
        out.append({
            'vmid': vmid,
            'name': str(r.get('name') or ''),
            'template': str(r.get('template', 0)) in ('1', 'True', 'true'),
            'status': str(r.get('status') or ''),
            'tags': parse_tags(r.get('tags')),
            'lock': str(r.get('lock') or ''),
        })
    out.sort(key=lambda v: v['vmid'])
    return out


def vm_config(node: str, vmid: int) -> dict:
    cfg = pvesh_json('/nodes/%s/qemu/%d/config' % (node, int(vmid)))
    return cfg if isinstance(cfg, dict) else {}


def find_os_disk_volid(cfg: dict) -> Optional[str]:
    """从 VM 配置中挑出系统盘卷 ID（跳过 cloudinit / cdrom / efi / tpm）。"""
    candidates = []
    for key, val in cfg.items():
        if not key.startswith(DISK_PREFIXES):
            continue
        prefix = next(p for p in DISK_PREFIXES if key.startswith(p))
        if not key[len(prefix):].isdigit():
            continue  # scsihw 之类
        val = str(val)
        if 'media=cdrom' in val or 'cloudinit' in val or val.split(',')[0].endswith('none'):
            continue
        volid = val.split(',')[0].strip()
        if volid and ':' in volid:
            candidates.append((key, volid))
    if not candidates:
        return None
    order = ''
    boot = str(cfg.get('boot', ''))
    if boot.startswith('order='):
        order = boot[len('order='):]
    elif cfg.get('bootdisk'):
        order = str(cfg['bootdisk'])
    for dev in [d for d in order.replace(',', ';').split(';') if d]:
        for key, volid in candidates:
            if key == dev:
                return volid
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def net_bridge(cfg: dict) -> Optional[str]:
    m = re.search(r'(?:^|,)bridge=([A-Za-z0-9_.-]+)', str(cfg.get('net0', '')))
    return m.group(1) if m else None


def linked_clone_refs(vmid: int, nodes_dir: str = None) -> list:
    """引用了模板基卷（base-<vmid>-disk-N）的其它 VM：链接克隆还在，基卷就不能删。

    直接读 pmxcfs（/etc/pve/nodes/*/qemu-server/*.conf），比逐台 qm config 快两个数量级，也覆盖集群里其它节点。
    """
    nodes_dir = nodes_dir or PVE_NODES_DIR
    pattern = re.compile(r'base-%d-disk-\d+' % int(vmid))
    refs = []
    with contextlib.suppress(OSError):
        for node in sorted(os.listdir(nodes_dir)):
            d = os.path.join(nodes_dir, node, 'qemu-server')
            with contextlib.suppress(OSError):
                for f in sorted(os.listdir(d)):
                    m = re.match(r'^(\d+)\.conf$', f)
                    if not m or int(m.group(1)) == int(vmid):
                        continue
                    with contextlib.suppress(OSError):
                        with open(os.path.join(d, f), 'r', errors='replace') as fh:
                            if pattern.search(fh.read()):
                                refs.append(int(m.group(1)))
    return refs


def allocate_vmid(used: set, lo: int, hi: int, high_water: int = 0) -> int:
    """区间内分配模板 VMID：游标式——从上次分配的号（high_water）之后往上找空闲号，到顶回绕，转一圈。

    不取最小空闲号：退役模板被回收后它的号会被别的系统复用，平台上只记了旧 VMID 的服务就会克隆到另一个系统
    （2026-11-05 审查 P1）。游标让同一个号要等整个区间（默认 9001-9999 共 999 个，集群内共享）转一圈才会再用到。
    还没有游标时从「区间内已用的最大号」之后开始，但不把区间顶端孤零零的号（常见的 9999 金模板）当起点，
    否则一开局就回绕成「取最小空闲号」。
    """
    lo, hi = int(lo), int(hi)
    in_range = sorted(v for v in used if lo <= v <= hi)
    if high_water and lo <= int(high_water) <= hi:
        start = int(high_water) + 1
    else:
        below_top = [v for v in in_range if v < hi - 9] or [lo - 1]
        start = below_top[-1] + 1
    if start > hi:
        start = lo
    for vid in list(range(start, hi + 1)) + list(range(lo, start)):
        if vid not in used:
            return vid
    raise PitError('no free VMID in %d-%d' % (lo, hi))


def allocate_vmid_top(used: set, lo: int, hi: int) -> int:
    """从区间顶端往下取空闲号：给开机冒烟这类用完即删的临时 VM，不推高模板的游标。"""
    for vid in range(int(hi), int(lo) - 1, -1):
        if vid not in used:
            return vid
    raise PitError('no free VMID in %d-%d' % (lo, hi))


# ---------------------------------------------------------------- 模板元数据

def build_tag(ts: float) -> str:
    return TAG_BUILD_PREFIX + time.strftime('%Y%m%d', time.gmtime(ts))


def managed_tags(ts: float, extra=()) -> list:
    return [TAG_MANAGED, build_tag(ts), *extra]


def encode_description(meta: dict) -> str:
    built = time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(meta.get('built_at', time.time())))
    lines = [
        'Managed by pve-import-template (%s). Rebuilt by sync-templates.py; do not edit by hand.' % meta.get('tool', '?'),
        'Built: %s  Source: %s' % (built, (meta.get('source') or {}).get('url', '?')),
        META_PREFIX + ' ' + json.dumps(meta, sort_keys=True, separators=(',', ':')),
    ]
    return '\n'.join(lines)


def decode_meta(description) -> Optional[dict]:
    for line in str(description or '').splitlines():
        line = line.strip()
        if line.startswith(META_PREFIX):
            with contextlib.suppress(ValueError):
                doc = json.loads(line[len(META_PREFIX):].strip())
                if isinstance(doc, dict) and doc.get('v') == META_VERSION:
                    return doc
    return None


def retired_name(name: str, ts: float) -> str:
    return '%s%s%s' % (name, RETIRED_INFIX, time.strftime('%Y%m%d%H%M', time.gmtime(ts)))


def retired_of(vm_name: str, name: str) -> bool:
    return vm_name.lower().startswith((name + RETIRED_INFIX).lower())


def is_retired(vm: dict) -> bool:
    return TAG_RETIRED in vm.get('tags', []) or RETIRED_INFIX in vm.get('name', '')


def gc_candidates(retired: list, keep: int) -> list:
    """retired 模板里该删的：只看本工具退役的（有 pit-retired tag、名字带可解析的时间戳），按时间倒序保留最新 keep 个。"""
    ours = [v for v in retired if TAG_RETIRED in v.get('tags', []) and retired_at(v.get('name', '')) > 0]
    ordered = sorted(ours, key=lambda v: (retired_at(v.get('name', '')), v['vmid']), reverse=True)
    return ordered[max(0, int(keep)):]


def retired_at(vm_name: str) -> int:
    """retired 名字里的 UTC 时间戳（<name>-retired-YYYYMMDDHHMM）；解析不出返回 0。"""
    import calendar
    m = re.search(re.escape(RETIRED_INFIX) + r'(\d{12})$', vm_name or '')
    if not m:
        return 0
    try:
        return calendar.timegm(time.strptime(m.group(1), '%Y%m%d%H%M'))
    except ValueError:
        return 0


def recipe_hash(template: dict, files: list, tool_version: str) -> str:
    """一个模板的「配方」指纹：yaml 条目 + 注入脚本内容 + 工具版本。配方变了（新的注入 / 调优）= 需要重建。"""
    h = hashlib.sha256()
    h.update(tool_version.encode())
    h.update(json.dumps({k: v for k, v in template.items() if k not in ('name',)}, sort_keys=True,
                        default=str).encode())
    for path in sorted(set(files)):
        h.update(os.path.relpath(path, SCRIPT_DIR).encode())
        with contextlib.suppress(OSError):
            with open(path, 'rb') as fh:
                h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- 上游：发现最新版 / 校验和

def http_open(url: str, method: str = 'GET', timeout: int = HTTP_TIMEOUT):
    req = urllib.request.Request(url, method=method, headers={'User-Agent': 'pve-import-template'})
    return urllib.request.urlopen(req, timeout=timeout)


def http_text(url: str, limit: int) -> str:
    with http_open(url) as resp:
        data = resp.read(limit + 1)
    if len(data) > limit:
        raise PitError('%s is larger than %d bytes' % (url, limit))
    return data.decode('utf-8', 'replace')


def http_head(url: str) -> dict:
    with http_open(url, method='HEAD') as resp:
        return {'etag': resp.headers.get('ETag'), 'last_modified': resp.headers.get('Last-Modified'),
                'length': resp.headers.get('Content-Length')}


def _version_key(groups) -> tuple:
    out = []
    for g in groups:
        out.append((0, int(g), '') if g is not None and g.isdigit() else (1, 0, g or ''))
    return tuple(out)


def version_key(fname: str, pattern: str) -> Optional[tuple]:
    """文件名在 discover pattern 下的版本键（捕获组按数字比较）；不匹配返回 None。"""
    m = re.fullmatch(pattern, fname or '')
    return _version_key(m.groups()) if m else None


def discover_latest(index_html: str, pattern: str) -> Optional[str]:
    """在目录索引里找匹配 pattern（锚定整个文件名）的最高版本文件名；捕获组按数字比较。

    同版本多个文件（CentOS 同一天两种命名并存）按文件名排序取定值——不能取决于 set() 的遍历顺序，
    否则每轮随机挑一个，天天判「上游换了」来回重建。
    """
    rx = re.compile(pattern)
    best = None
    for href in sorted(set(re.findall(r'href="([^"?#]+)"', index_html))):
        fname = urllib.parse.unquote(href.rsplit('/', 1)[-1])
        m = rx.fullmatch(fname)
        if not m:
            continue
        cand = (_version_key(m.groups()), fname)
        if best is None or cand > best:
            best = cand
    return best[1] if best else None


def resolve_source(template: dict, log=None, config: Optional[dict] = None, last_known: Optional[str] = None) -> tuple:
    """(url, how)。how：pinned（没配 discover）/ discovered / fallback（目录索引都失败）。

    配了 discover 就去目录索引找最新点版本（主站不通时依次试 image_mirrors）。都失败时回退到
    **上次成功用过的地址**（last_known），没有才用 yaml 里钉死的 url——否则索引临时打不开会被当成
    「上游换了」，用更旧的点版本重建（降级）。
    """
    disc = template.get('discover')
    if not disc or not disc.get('index') or not disc.get('pattern'):
        return template['url'], 'pinned'
    index, pattern = disc['index'], disc['pattern']
    errors = []
    known_key = version_key((last_known or '').rsplit('/', 1)[-1], pattern) if last_known else None
    for candidate in mirror_candidates(index, config):
        try:
            fname = discover_latest(http_text(candidate, INDEX_MAX_BYTES), pattern)
        except (OSError, urllib.error.URLError, PitError, re.error) as e:
            errors.append('%s: %s' % (candidate, e))
            continue
        if fname:
            # 只往上走：落后的备用站 / 缓存的旧索引给出的更低版本不算「上游换了」，沿用在役的那个
            if known_key is not None and version_key(fname, pattern) < known_key:
                return last_known, 'discovered'
            # 下载地址仍以主站为准（备用站由下载环节按同样规则改写），元数据里的 url 才稳定可比
            return urllib.parse.urljoin(index if index.endswith('/') else index + '/', fname), 'discovered'
        errors.append('%s: no file matches' % candidate)
    fallback = last_known or template['url']
    if log:
        log('Warning: discover for %s failed (%s); using %s' % (template['name'], '; '.join(errors)[:300], fallback))
    return fallback, 'fallback'


def resolve_source_url(template: dict, log=None, config: Optional[dict] = None, last_known: Optional[str] = None) -> str:
    return resolve_source(template, log, config, last_known)[0]


def mirror_candidates(url: str, config: Optional[dict]) -> list:
    """主站地址 + templates.yaml image_mirrors 里按前缀改写出的备用地址（主站下载失败时依次尝试）。"""
    out = [url]
    for rule in (config or {}).get('image_mirrors') or []:
        prefix = str(rule.get('from') or '')
        if not prefix or not url.startswith(prefix):
            continue
        targets = rule.get('to') or []
        for target in ([targets] if isinstance(targets, str) else targets):
            alt = str(target) + url[len(prefix):]
            if alt not in out:
                out.append(alt)
    return out


def checksum_url(template: dict, url: str, index_html: Optional[str] = None) -> Optional[str]:
    """校验文件地址：checksum 支持 {url} / {dir} 占位；文件名随构建号变的（Fedora）用 checksum_pattern 在目录里找。"""
    spec = template.get('checksum')
    directory = url.rsplit('/', 1)[0]
    if spec:
        return str(spec).replace('{url}', url).replace('{dir}', directory)
    pattern = template.get('checksum_pattern')
    if pattern:
        html = index_html if index_html is not None else http_text(directory + '/', INDEX_MAX_BYTES)
        fname = discover_latest(html, pattern)
        return directory + '/' + fname if fname else None
    return None


HASH_ALGOS = {64: 'sha256', 128: 'sha512'}


def parse_checksum(text: str, filename: str) -> Optional[tuple]:
    """(algo, hex)。支持 GNU（hash  file / hash *file）、BSD（SHA256 (file) = hash）、只有一个 hash 的单文件。"""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith('#')]
    for line in lines:
        m = re.match(r'^(SHA256|SHA512)\s*\((.+)\)\s*=\s*([0-9a-fA-F]+)$', line)
        if m and os.path.basename(m.group(2)) == filename:
            return HASH_ALGOS.get(len(m.group(3))), m.group(3).lower()
        m = re.match(r'^([0-9a-fA-F]{64}|[0-9a-fA-F]{128})\s+\*?(.+)$', line)
        if m and os.path.basename(m.group(2).strip()) == filename:
            return HASH_ALGOS[len(m.group(1))], m.group(1).lower()
    if len(lines) == 1 and re.fullmatch(r'[0-9a-fA-F]{64}|[0-9a-fA-F]{128}', lines[0].split()[0]):
        h = lines[0].split()[0]
        return HASH_ALGOS[len(h)], h.lower()
    return None


def file_digest(path: str, algo: str) -> str:
    h = hashlib.new(algo)
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- virt-customize

def run_virt_customize(image: str, args: list, *, timeout: int = 3600, force_tcg: bool = False,
                       debug: bool = False, log=None, prepare=None, fmt: Optional[str] = None) -> str:
    """运行 virt-customize；appliance 起不来（LaunchError）且没强制 TCG 时自动用 force_tcg 再试一次。

    脚本本身出错（CommandError）不重跑：定制步骤不保证幂等，重跑会把 sed 之类的改动叠两遍。
    """
    def attempt(tcg: bool) -> str:
        env = {'LIBGUESTFS_BACKEND': 'direct'}
        if tcg:
            env['LIBGUESTFS_BACKEND_SETTINGS'] = 'force_tcg'
        if debug:
            env.update({'LIBGUESTFS_DEBUG': '1', 'LIBGUESTFS_TRACE': '1'})
        if prepare is not None:
            prepare()  # 每次尝试都从干净副本开始：上一次可能已改了一半（TCG 重试不能叠在半成品上）
        fmt_args = ['--format', fmt] if fmt else []
        try:
            return run_cmd(['virt-customize', *fmt_args, '-a', image, *args], timeout=timeout, env=env, log=log)
        except CommandError as e:
            lowered = e.output.lower()
            if e.returncode != 124 and any(m in lowered for m in LAUNCH_FAILURE_MARKERS):
                raise LaunchError('libguestfs appliance failed to start', e.output) from None
            raise

    try:
        return attempt(force_tcg)
    except LaunchError:
        if force_tcg:
            raise
        if log:
            log('virt-customize: appliance failed to start with KVM; retrying with force_tcg')
        return attempt(True)


# ---------------------------------------------------------------- 配置文件 / 状态文件

def parse_conf_text(text: str, schema: dict) -> dict:
    """KEY=VALUE（可带一层引号）→ dict。只认 schema 里的键，值按正则校验；不经 shell、不展开变量。

    schema: {KEY: (regex, default)}。非法值抛 PitError（宁可不跑，也不按半份配置跑）。
    """
    out = {k: d for k, (_rx, d) in schema.items()}
    for n, raw in enumerate(text.split('\n'), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        m = re.match(r'^([A-Z][A-Z0-9_]*)=(.*)$', line)
        if not m:
            raise PitError('line %d: expected KEY=VALUE' % n)
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
            val = val[1:-1]
        if key not in schema:
            continue
        rx, _default = schema[key]
        if not re.fullmatch(rx, val):
            raise PitError('line %d: invalid value for %s' % (n, key))
        out[key] = val
    return out


def write_json_atomic(path: str, doc: dict, mode: int = 0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = '%s.tmp.%d' % (path, os.getpid())
    with open(tmp, 'w') as fh:
        json.dump(doc, fh, sort_keys=True, indent=1)
        fh.write('\n')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def prune_files(directory: str, prefix: str, keep: int):
    with contextlib.suppress(OSError):
        files = sorted((f for f in os.listdir(directory) if f.startswith(prefix)), reverse=True)
        for f in files[keep:]:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(directory, f))


def name_matches(name: str, selector: str) -> bool:
    """模板名匹配：大小写不敏感（节点上 AlpineLinux-3.20.3 / alpineLinux-3.23 混着叫）。"""
    if any(ch in selector for ch in '*?['):
        return fnmatch.fnmatch(name.lower(), selector.lower())
    return name.lower() == selector.lower()


def expand_profiles(selector: Optional[str], config: dict) -> Optional[str]:
    """选择器里的 @profile 展开成 templates.yaml profiles 里的名字 / glob（小硬盘节点只维护几个主流模板）。"""
    if not selector:
        return selector
    profiles = config.get('profiles') or {}
    out = []
    for token in [t.strip() for t in selector.split(',') if t.strip()]:
        if not token.startswith('@'):
            out.append(token)
            continue
        items = profiles.get(token[1:])
        if items is None:
            raise PitError('unknown profile %s (defined: %s)' % (token, ', '.join(sorted(profiles)) or 'none'))
        out.extend([items] if isinstance(items, str) else [str(i) for i in items])
    return ','.join(out)


# ---------------------------------------------------------------- 磁盘空间

GIB = 1024 ** 3


FILE_STORAGE_TYPES = ('dir', 'nfs', 'cifs', 'glusterfs', 'btrfs')


def storage_type(storage: str) -> Optional[str]:
    with contextlib.suppress(PitError, ValueError, TypeError):
        st = pvesh_json('/storage/%s' % storage)
        if isinstance(st, dict):
            return st.get('type')
    return None


def disk_format_for(storage: str, current_volid: Optional[str] = None) -> Optional[str]:
    """新模板系统盘的格式：沿用旧模板（.qcow2 就 qcow2）；文件类存储默认 qcow2——import-from 在 dir / NFS 上默认 raw，
    raw 盘的客户 VM 做不了快照（旧版 importdisk 写死 --format qcow2）。块存储（lvm / zfs / rbd）没有格式之分，返回 None。"""
    if current_volid and current_volid.endswith('.qcow2'):
        return 'qcow2'
    if current_volid and current_volid.endswith('.raw'):
        return 'raw'
    return 'qcow2' if storage_type(storage) in FILE_STORAGE_TYPES else None


def is_eol(template: dict, today: Optional[str] = None) -> bool:
    """eol: true / false，或 'YYYY-MM-DD'（到这天就算 EOL，工具按日期自动判定，不用每次手改 yaml）。"""
    value = template.get('eol')
    if isinstance(value, bool):
        return value
    if value is None or value == '':
        return False
    today = today or time.strftime('%Y-%m-%d', time.gmtime())
    return str(value) <= today


# 新模板要沿用旧模板的节点策略键（非磁盘 / 非网卡）。hookscript 最关键：平台用 API token，PVE 只许 root 设 hookscript，
# 客户 VM 的 hook（VRF 路由注入 / cicustom / BCP38 / 端口隔离）全靠从模板克隆继承——新模板不带，VRF 节点上新开的 VM 就没网
CARRY_OVER_KEYS = {
    'hookscript': r'[A-Za-z0-9_.-]+:snippets/[A-Za-z0-9_.-]+',
    'vga': r'[A-Za-z0-9=,._-]{1,64}',
    'machine': r'[A-Za-z0-9=,.+_-]{1,64}',
}


def carry_over(cfg: dict) -> dict:
    """从旧模板配置里取要沿用的键（逐个按白名单正则校验，怪值丢弃）。"""
    out = {}
    for key, rx in CARRY_OVER_KEYS.items():
        value = str(cfg.get(key) or '').strip()
        if value and re.fullmatch(rx, value):
            out[key] = value
    return out


def inspect_image(path: str) -> str:
    """下载的镜像来自外部（备用站时完整性只与备用站一样可信）：只收 qcow2 / raw，且不得带 backing file / 外部 data-file——
    被篡改的 qcow2 可以把 backing 指向宿主机文件或客户磁盘，virt-customize 打开、import-from 扁平化后就进了模板。返回格式。"""
    info = json.loads(run_cmd(['qemu-img', 'info', '--output', 'json', '-U', path], timeout=120))
    fmt = info.get('format')
    if fmt not in ('qcow2', 'raw'):
        raise PitError('unexpected image format %r' % fmt)
    if info.get('backing-filename') or info.get('full-backing-filename'):
        raise PitError('image has a backing file (%s); refusing' % (info.get('backing-filename') or info.get('full-backing-filename')))
    data = ((info.get('format-specific') or {}).get('data') or {})
    if data.get('data-file'):
        raise PitError('image references an external data file; refusing')
    return fmt


def free_bytes(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def image_sizes(path: str) -> tuple:
    """(virtual, actual) 字节数；qemu-img 不可用时退回文件大小。"""
    actual = os.path.getsize(path)
    with contextlib.suppress(PitError, ValueError, KeyError):
        info = json.loads(run_cmd(['qemu-img', 'info', '--output', 'json', path], timeout=120))
        return int(info['virtual-size']), int(info.get('actual-size') or actual)
    return actual, actual


def storage_avail(node: str, storage: str) -> Optional[int]:
    with contextlib.suppress(PitError, ValueError, TypeError):
        st = pvesh_json('/nodes/%s/storage/%s/status' % (node, storage))
        if isinstance(st, dict) and st.get('avail') is not None:
            return int(st['avail'])
    return None


def ensure_space(what: str, avail: Optional[int], needed: int):
    if avail is not None and avail < needed:
        raise PitError('insufficient space on %s: %.1f GiB free, need %.1f GiB' % (what, avail / GIB, needed / GIB))
