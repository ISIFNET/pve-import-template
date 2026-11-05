#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync-templates.py —— 让本节点的模板保持最新：新建 → 切换 → 回收（蓝绿），非交互，可 JSON 输出。

为什么不就地改模板（update-templates.py）：模板基卷被链接克隆共享（qcow2 backing / ZFS 快照），
就地写要么被 PVE 设为只读拒写、要么改了也不进新克隆、要么改坏已有克隆。这里每次都**新建**一个模板，
构建完全成功后才接管名字，旧模板改名为 <name>-retired-<时间> 并打 pit-retired 标记，过了宽限期、
确认没有链接克隆引用后再删。任何一步失败，旧模板都原样在役。

什么时候重建一个模板（按优先级）：
  forced          显式 --force
  source-changed  上游镜像变了（discover 找到新点版本 / ETag / Last-Modified 变化）
  stale           距上次构建超过 MAX_AGE_DAYS（镜像没变也要重新跑包升级拿安全补丁）
  recipe-changed  注入方式变了（templates.yaml 条目 / uploads 脚本 / 工具版本 / 开关）
  legacy          旧工具导入的模板，没有构建元数据
  missing         本节点还没有（仅 --add-missing）

用法
  python3 sync-templates.py [选择器 ...] [选项]

  选择器：模板名 / glob / 系列名（同 import.py），大小写不敏感；不写 = 配置里的 SELECTOR，再不写 = 全部
  默认只处理本节点**已有**的模板；--add-missing 才会导入缺的（需要 --storage 或配置 STORAGE）。

选项
  --config <path>     配置文件（默认 /etc/pve-import-template.conf，不存在则用内置默认值）
  --force             不看是否需要，选中的全部重建
  --add-missing       也导入本节点还没有的模板
  --storage <name>    新导入模板用的存储（重建沿用旧模板所在存储）
  --max-builds <n>    本次最多构建几个（其余记 deferred，下次再建；0 = 不限）
  --max-age-days <n>  超过多少天的构建视为过期（0 = 不按年龄重建）
  --keep <n>          每个模板保留几个已退役版本（用于回滚，默认 1）
  --mirror <name>     镜像内换源（templates.yaml 的 mirrors）
  --no-upgrade        不做包升级
  --no-tuning         不注入网络调优
  --tcg               强制 libguestfs 软件模拟
  --insecure          下载时跳过 TLS 校验
  --dry-run           只打印计划（会探测上游，不构建、不改任何东西）
  --json              stdout 只输出一份 JSON 报告（日志走 stderr）
  --list              列出本节点模板及其构建信息后退出（配合 --json 输出 JSON）
  -h, --help

退出码：0 全部成功 / 1 有模板失败 / 2 用法或依赖错误 / 3 另一个实例在跑 / 130 被中断
"""

import argparse
import contextlib
from typing import Optional
import importlib.util
import json
import os
import re
import shutil
import sys
import time

import pitlib

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONF = '/etc/pve-import-template.conf'
STATE_FILE = os.path.join(pitlib.STATE_DIR, 'last-run.json')
BUILD_SUFFIX = '-pitbuild'
# 刚退役的模板至少留这么久：平台的模板快照每 15 分钟刷新一次，快照里还指着旧 VMID 的克隆要能继续
RETIRE_GRACE_SECONDS = 12 * 3600
# 退役改名撞上正在进行的克隆（clone 持有源模板的锁）时的重试
RETIRE_ATTEMPTS = 10
RETIRE_RETRY_SECONDS = 30
LOG_KEEP = 30

CONF_SCHEMA = {
    'STORAGE': (r'(|[A-Za-z0-9][A-Za-z0-9_.-]{0,63})', ''),
    'BRIDGE': (r'(|[A-Za-z0-9][A-Za-z0-9_.-]{0,31})', ''),
    'VMID_MIN': (r'[0-9]{3,9}', '9001'),
    'VMID_MAX': (r'[0-9]{3,9}', '9999'),
    'MAX_AGE_DAYS': (r'[0-9]{1,4}', '30'),
    'MAX_BUILDS': (r'[0-9]{1,3}', '3'),
    'KEEP_RETIRED': (r'[0-9]{1,2}', '1'),
    # 0 = 构建完删掉缓存的上游镜像（只留元数据），小硬盘节点不常驻十几 GB 缓存
    'KEEP_CACHE': (r'[01]', '0'),
    'MIRROR': (r'[A-Za-z0-9_-]{0,32}', ''),
    'UPGRADE': (r'[01]', '1'),
    'NET_TUNING': (r'[01]', '1'),
    # 维护哪些模板：PROFILE（templates.yaml profiles 里的名字）或 SELECTOR（名字 / glob / 系列，可含 @profile）；
    # 都不写 = templates.yaml 里的全部（但默认只维护本节点已有的，ADD_MISSING=1 才补缺的）
    'PROFILE': (r'[a-z0-9_-]{0,32}', ''),
    'SELECTOR': (r'[A-Za-z0-9.*?,_@-]{0,200}', ''),
    'ADD_MISSING': (r'[01]', '0'),
    # 1 = 旧工具导入的（没有构建元数据的）同名模板也按 yaml 重建接管；0 = 只维护本工具建的，旧模板原样不动
    'ADOPT_LEGACY': (r'[01]', '1'),
    # 20 而不是 24：timer 每天一次还带 0-3h 随机延迟，24h 窗口一半情况要拖到第三天才重试
    'BACKOFF_HOURS': (r'[0-9]{1,4}', '20'),
    # 切换前的开机冒烟（克隆一台不接网卡的临时 VM，等 qemu-guest-agent 应答）；节点是嵌套 / TCG 时调大超时
    'SMOKE_TEST': (r'[01]', '1'),
    'SMOKE_TIMEOUT': (r'[0-9]{2,4}', '600'),
}

REASON_PRIORITY = {'forced': 0, 'source-changed': 1, 'stale': 2, 'recipe-changed': 3, 'legacy': 4, 'missing': 5}
# 上游 404（镜像被删 / 改名）不是隔天就会好的事：等 templates.yaml 更新（url 变了就不再退避）
NOT_FOUND_BACKOFF = 7 * 86400
MAX_BACKOFF = 7 * 86400

_LOG_FH = None
# 本轮的报告（含退避记录与 VMID 游标）：被中断时存这份而不是上一轮的文件
_REPORT = None


def log(msg: str):
    line = '[%s] %s' % (time.strftime('%H:%M:%S'), msg)
    print(line, file=sys.stderr, flush=True)
    if _LOG_FH is not None:
        with contextlib.suppress(OSError):
            _LOG_FH.write(line + '\n')
            _LOG_FH.flush()


def open_log() -> str:
    global _LOG_FH
    os.makedirs(pitlib.LOG_DIR, mode=0o700, exist_ok=True)
    os.chmod(pitlib.LOG_DIR, 0o700)
    pitlib.prune_files(pitlib.LOG_DIR, 'sync-', LOG_KEEP - 1)
    path = os.path.join(pitlib.LOG_DIR, 'sync-%s.log' % time.strftime('%Y%m%d-%H%M%S'))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    _LOG_FH = os.fdopen(fd, 'w')
    return path


def load_import_module():
    """加载 import.py（文件名与关键字冲突，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location('pveimport', os.path.join(SCRIPT_DIR, 'import.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_conf(path: str, explicit: bool) -> dict:
    if not os.path.exists(path):
        if explicit:
            raise pitlib.PitError('config file %s does not exist' % path)
        return pitlib.parse_conf_text('', CONF_SCHEMA)
    with open(path, 'r', encoding='utf-8', errors='strict') as fh:
        text = fh.read()
    try:
        return pitlib.parse_conf_text(text, CONF_SCHEMA)
    except pitlib.PitError as e:
        raise pitlib.PitError('%s: %s' % (path, e)) from None


def parse_args(argv):
    # allow_abbrev=False：`--insec` 之类的缩写不认（选择器 / 平台参数不能拼出意外的选项）
    p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    p.add_argument('selectors', nargs='*')
    p.add_argument('--config')
    p.add_argument('--force', action='store_true')
    p.add_argument('--add-missing', action='store_true')
    p.add_argument('--storage')
    p.add_argument('--max-builds', type=int)
    p.add_argument('--max-age-days', type=int)
    p.add_argument('--keep', type=int)
    p.add_argument('--mirror')
    p.add_argument('--no-upgrade', action='store_true')
    p.add_argument('--no-tuning', action='store_true')
    p.add_argument('--tcg', action='store_true')
    p.add_argument('--insecure', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--json', action='store_true')
    p.add_argument('--list', action='store_true')
    p.add_argument('-h', '--help', action='store_true')
    args = p.parse_args(argv)
    for name in ('max_builds', 'max_age_days', 'keep'):
        v = getattr(args, name)
        if v is not None and v < 0:
            p.error('--%s must be >= 0' % name.replace('_', '-'))
    if args.storage and not re.fullmatch(CONF_SCHEMA['STORAGE'][0], args.storage):
        p.error('--storage has an invalid value')
    for sel in args.selectors:
        if not re.fullmatch(r'[A-Za-z0-9@*?][A-Za-z0-9.*?,_@-]{0,199}', sel):
            p.error('invalid selector %r' % sel)
    return args


# ---------------------------------------------------------------- 节点现状

def node_inventory(node: str) -> list:
    """本节点模板：[{vmid, name, tags, lock, retired, meta, config}]（只读模板的配置，普通 VM 不读）。"""
    out = []
    for vm in pitlib.node_vms(node):
        if not vm['template']:
            continue
        cfg = {}
        with contextlib.suppress(pitlib.PitError, ValueError):
            cfg = pitlib.vm_config(node, vm['vmid'])
        vm = dict(vm)
        vm['config'] = cfg
        vm['meta'] = pitlib.decode_meta(cfg.get('description'))
        vm['retired'] = pitlib.is_retired(vm)
        out.append(vm)
    return out


def in_range(vmid: int, conf: dict) -> bool:
    return int(conf['VMID_MIN']) <= int(vmid) <= int(conf['VMID_MAX'])


def cleanup_stale_builds(node: str, conf: dict, dry_run: bool):
    """上次被强杀留下的半成品，逐条核对是本工具的东西才删（客户 VM 名是 VM-<id>，但防线不能只靠名字）：

    - 构建中的 VM / 已转模板但没来得及改名的 `<name>-pitbuild`：区间内、带 pit tag、description 的 pit-meta 名字对得上；
    - 开机冒烟留下的 `pitsmoke-<vmid>`：带 pit-smoke tag（可能还开着，先停）；
    - 镜像缓存里的工作副本（每份几 GB）。
    """
    # 冒烟克隆先删（它是 -pitbuild 模板的链接克隆，不先删它模板删不掉）
    vms = sorted(pitlib.node_vms(node), key=lambda v: (not v['name'].startswith('pitsmoke-'), v['vmid']))
    for vm in vms:
        if vm['lock'] or not in_range(vm['vmid'], conf):
            continue
        # clone 之后、打 pit-smoke 之前被杀的冒烟 VM 带的是继承来的 pit 标签
        smoke = vm['name'].startswith('pitsmoke-') and (pitlib.TAG_SMOKE in vm['tags'] or pitlib.TAG_MANAGED in vm['tags'])
        build = vm['name'].endswith(BUILD_SUFFIX) and pitlib.TAG_MANAGED in vm['tags']
        if not (smoke or build):
            continue
        if build:
            cfg = {}
            with contextlib.suppress(pitlib.PitError, ValueError):
                cfg = pitlib.vm_config(node, vm['vmid'])
            meta = pitlib.decode_meta(cfg.get('description')) or {}
            if str(meta.get('name', '')) + BUILD_SUFFIX != vm['name']:
                continue
        log('removing leftover %s VM %d (%s)' % ('smoke-test' if smoke else 'build', vm['vmid'], vm['name']))
        if dry_run:
            continue
        if vm['status'] == 'running':
            with contextlib.suppress(pitlib.PitError):
                pitlib.run_cmd(['qm', 'stop', vm['vmid'], '--timeout', '30'], timeout=120, log=log)
        with contextlib.suppress(pitlib.PitError):
            pitlib.run_cmd(['qm', 'destroy', vm['vmid'], '--purge', '1'], timeout=600, log=log)
    work = os.path.join(os.environ.get('CLOUD_IMG_DIR') or os.path.join(SCRIPT_DIR, 'cloud_img'), 'work')
    if not dry_run and os.path.isdir(work):
        for f in os.listdir(work):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(work, f))


def current_of(actives: list) -> dict:
    """同名在役模板里「当前」那个：构建时间最新，其次 VMID 最大。"""
    return sorted(actives, key=lambda v: ((v['meta'] or {}).get('built_at', 0), v['vmid']), reverse=True)[0]


retired_at = pitlib.retired_at


# ---------------------------------------------------------------- 决策

def failure_kind(exc: BaseException) -> str:
    kind = getattr(exc, 'kind', None)
    if kind:
        return str(kind)
    if isinstance(exc, pitlib.LaunchError):
        return 'appliance'
    if 'insufficient space' in str(exc):
        return 'space'
    if 'smoke test failed' in str(exc):
        return 'smoke'
    return 'build'


def backoff_remaining(last_fail: dict, tpl: dict, recipe: str, base: int, now: float) -> int:
    """上次失败后还要等多少秒。配方或 yaml 里的 url / discover 变了（有人修过了）立即重试；
    404 固定退避一周；其它按连续失败次数翻倍（base, 2×base, …，封顶一周）。"""
    if not last_fail or last_fail.get('recipe') != recipe or last_fail.get('url') != source_key(tpl):
        return 0
    if last_fail.get('kind') == 'not-found':
        window = NOT_FOUND_BACKOFF
    else:
        window = min(base * (2 ** max(0, int(last_fail.get('count', 1)) - 1)), MAX_BACKOFF)
    return max(0, int(float(last_fail.get('at', 0)) + window - now))


def source_key(tpl: dict) -> str:
    """yaml 里决定「去哪下载」的部分（url + discover），改了就该立即重试。"""
    return json.dumps([tpl.get('url'), tpl.get('discover')], sort_keys=True)


def decide(tpl: dict, current: dict, *, imp, force: bool, max_age_days: int, now: float, recipe: str,
           config: dict) -> tuple:
    """(reason, detail, url, warnings)；reason=None 表示不需要重建。只做只读探测（目录索引 / 校验文件 / HEAD）。

    url 是构建要用的下载地址——决策定了就传给构建，不让构建再解析一次（两次之间索引可能变了）。
    """
    meta = current.get('meta') or {}
    src = meta.get('source') or {}
    warnings = []
    url, how = pitlib.resolve_source(tpl, log=log, config=config, last_known=src.get('url'))
    if how == 'fallback':
        warnings.append('discover failed (upstream renamed its files?); kept %s' % url.rsplit('/', 1)[-1])
    if force:
        return 'forced', 'requested', url, warnings
    if not current.get('meta'):
        return 'legacy', 'no build metadata (imported by an older tool)', url, warnings
    if how != 'fallback' and url != src.get('url'):
        return 'source-changed', 'new upstream image %s' % url.rsplit('/', 1)[-1], url, warnings
    if max_age_days > 0 and now - float(meta.get('built_at', 0)) > max_age_days * 86400:
        return 'stale', 'built %d days ago' % ((now - float(meta.get('built_at', 0))) // 86400), url, warnings
    if meta.get('recipe') != recipe:
        return 'recipe-changed', 'injection recipe changed', url, warnings
    # 同一个地址重新发布：有校验文件就比上游 hash（HEAD 会被 Fedora / openSUSE / Arch 重定向到不同镜像站，
    # ETag 每次都不一样，按 ETag 判会天天重建）；没有校验文件才退回 ETag / Last-Modified
    if src.get('checksum') and (tpl.get('checksum') or tpl.get('checksum_pattern')):
        upstream = imp.upstream_checksum(tpl, url, config)
        if upstream and upstream != src['checksum']:
            return 'source-changed', 'upstream image was republished (checksum changed)', url, warnings
        return None, 'up to date', url, warnings
    try:
        head = pitlib.http_head(url)
        if (head.get('etag') or head.get('last_modified')) and not imp.source_unchanged(src, head):
            return 'source-changed', 'upstream image was republished', url, warnings
    except Exception as e:  # noqa: BLE001 - 探测失败不判「变了」，只告警
        log('Warning: HEAD %s failed (%s); assuming unchanged' % (url, e))
    return None, 'up to date', url, warnings


class Skip(pitlib.PitError):
    """不能（也不该）自动重建的模板：记 skipped，不算失败、不进退避。"""


def retire(old: dict, name: str, log_warnings: list) -> bool:
    """旧模板改名 <name>-retired-<此刻> 并打 pit-retired。平台正在从它全克隆时 clone 持有它的锁，qm set 会超时：
    退避重试一阵（克隆一般几分钟），实在不行留给下一轮（同名多个在役时 run_sync 会补退役）。"""
    tags = pitlib.format_tags(old['tags'] + [pitlib.TAG_RETIRED])
    for attempt in range(RETIRE_ATTEMPTS):
        # 时间戳取退役这一刻：宽限期从这里算（旧版用构建开始的时间，构建一小时后改名，keep=0 时当场就被回收）
        new_name = pitlib.retired_name(name, time.time())
        try:
            pitlib.run_cmd(['qm', 'set', old['vmid'], '--name', new_name, '--tags', tags], timeout=120, log=log)
            return True
        except pitlib.PitError as e:
            if attempt == RETIRE_ATTEMPTS - 1:
                log_warnings.append('could not retire VM %d: %s' % (old['vmid'], e))
                log('Warning: ' + log_warnings[-1])
                return False
            time.sleep(RETIRE_RETRY_SECONDS)
    return False


def node_hookscript(inventory: list, conf: dict) -> dict:
    """新导入的模板沿用本节点在役模板最常见的 hookscript（客户 VM 从模板克隆继承 hook；API token 设不了 hookscript）。"""
    counts = {}
    for vm in inventory:
        if in_range(vm['vmid'], conf) and not vm['retired']:
            hook = pitlib.carry_over(vm['config']).get('hookscript')
            if hook:
                counts[hook] = counts.get(hook, 0) + 1
    return {'hookscript': max(counts, key=counts.get)} if counts else {}


def build_one(imp, tpl: dict, node: str, actives: list, *, storage: str, used: set, conf: dict, opts: dict,
              mirror_config, mirror_name, url: str, state: dict, config: Optional[dict] = None,
              inventory: Optional[list] = None, current_volid: str = '') -> dict:
    """构建一个新模板并切换。返回结果行（action=built）；失败抛异常（旧模板不受影响）。"""
    name = tpl['name']
    vm_opts = {}
    current = current_of(actives) if actives else None
    # 节点策略键（hookscript / vga / machine）照抄旧模板；新导入的取本节点模板最常见的 hookscript
    extra_config = pitlib.carry_over(current['config']) if current is not None else node_hookscript(inventory or [], conf)
    if current is not None:
        cfg = current['config']
        if str(cfg.get('bios', 'seabios')) != 'seabios':
            raise Skip('%s (VM %d) uses %s firmware; only SeaBIOS templates are rebuilt automatically'
                       % (name, current['vmid'], cfg.get('bios')))
        bridge = pitlib.net_bridge(cfg)
        if bridge:
            vm_opts['bridge'] = bridge
        for key in ('memory', 'cores'):
            if str(cfg.get(key, '')).isdigit():
                vm_opts[key] = int(cfg[key])
        if cfg.get('cpu'):
            vm_opts['cpu'] = str(cfg['cpu'])
    elif conf.get('BRIDGE'):
        vm_opts['bridge'] = conf['BRIDGE']

    def allocate() -> int:
        # qm create 前一刻才分配（构建要跑很久，集群里别的节点可能刚占了号）；单调往上，不复用回收掉的号
        used.update(pitlib.cluster_used_vmids())
        vid = pitlib.allocate_vmid(used, int(conf['VMID_MIN']), int(conf['VMID_MAX']), int(state.get('vmid_high_water', 0)))
        used.add(vid)
        state['vmid_high_water'] = max(int(state.get('vmid_high_water', 0)), vid)
        return vid

    def allocate_smoke() -> int:
        # 冒烟临时 VM 用完即删：从区间顶端往下取，不推高模板游标
        used.update(pitlib.cluster_used_vmids())
        vid = pitlib.allocate_vmid_top(used, int(conf['VMID_MIN']), int(conf['VMID_MAX']))
        used.add(vid)
        return vid

    expected = imp.upstream_checksum(tpl, url, config) if (tpl.get('checksum') or tpl.get('checksum_pattern')) else None
    log('building %s on storage %s from %s' % (name, storage, url.rsplit('/', 1)[-1]))
    meta = imp.build_template(tpl, storage, allocate, vm_name=name + BUILD_SUFFIX, mirror_config=mirror_config,
                              mirror_name=mirror_name, apply_tuning=opts['tuning'], upgrade=opts['upgrade'],
                              vm_opts=vm_opts, force_tcg=opts['tcg'], keep_cache=opts['keep_cache'],
                              node=node, log=log, source_url=url, smoke_timeout=opts['smoke_timeout'],
                              smoke_allocate=allocate_smoke, extra_config=extra_config,
                              disk_format=pitlib.disk_format_for(storage, current_volid or None),
                              expected_checksum=expected, require_checksum=True)
    vmid = int(meta['vmid'])
    if current is not None and 'hookscript' in extra_config and 'hookscript' not in meta.get('carried', []):
        # 防御：旧模板有 hookscript、新的没带上就不切（客户 VM 会没有 hook）
        raise pitlib.PitError('new template lost the hookscript of the one it replaces; not switching')

    # 切换：先让新模板接管名字，再把旧的改名退役——两步之间两个同名模板都在，克隆哪个都能用，没有空窗
    pitlib.run_cmd(['qm', 'set', vmid, '--name', name], timeout=120, log=log)
    retired, warnings = [], []
    for old in actives:
        if retire(old, name, warnings):
            retired.append(old['vmid'])
    return {'action': 'built', 'new_vmid': vmid, 'retired': retired, 'meta': meta, 'warnings': warnings}


def collect_garbage(node: str, name: str, keep: int, now: float, dry_run: bool) -> tuple:
    """(deleted, kept_notes)：同名 retired 模板保留最新 keep 个，其余过了宽限期、没有链接克隆、没保护的删除。
    只认本工具退役的（pit-retired + 名字里能解析出退役时间），运维手工起名的 *-retired-old 永远不动。"""
    retired = [vm for vm in pitlib.node_vms(node) if vm['template'] and pitlib.retired_of(vm['name'], name)]
    deleted, notes = [], []
    for vm in pitlib.gc_candidates(retired, keep):
        if now - retired_at(vm['name']) < RETIRE_GRACE_SECONDS:
            notes.append('%d: retired less than %d h ago' % (vm['vmid'], RETIRE_GRACE_SECONDS // 3600))
            continue
        if vm['lock']:
            notes.append('%d: locked (%s)' % (vm['vmid'], vm['lock']))
            continue
        cfg = {}
        with contextlib.suppress(pitlib.PitError, ValueError):
            cfg = pitlib.vm_config(node, vm['vmid'])
        if str(cfg.get('protection', '0')) in ('1', 'true'):
            notes.append('%d: protection is on' % vm['vmid'])
            continue
        refs = pitlib.linked_clone_refs(vm['vmid'])
        if refs:
            notes.append('%d: still used by linked clones %s' % (vm['vmid'], ','.join(map(str, refs[:10]))))
            continue
        log('deleting retired template %d (%s)' % (vm['vmid'], vm['name']))
        if dry_run:
            deleted.append(vm['vmid'])
            continue
        try:
            # 不带 --destroy-unreferenced-disks：模板的盘都在配置里；那个参数按 VMID 扫存储，共挂存储的单机节点会误删别家同号盘
            pitlib.run_cmd(['qm', 'destroy', vm['vmid'], '--purge', '1'], timeout=900, log=log)
            deleted.append(vm['vmid'])
        except pitlib.PitError as e:
            notes.append('%d: destroy failed: %s' % (vm['vmid'], e))
    return deleted, notes


# ---------------------------------------------------------------- 状态文件

def load_state() -> dict:
    with contextlib.suppress(OSError, ValueError):
        with open(STATE_FILE) as fh:
            doc = json.load(fh)
            if isinstance(doc, dict):
                return doc
    return {}


def save_state(doc: dict):
    with contextlib.suppress(OSError):
        pitlib.write_json_atomic(STATE_FILE, doc)


def record_failure(message: str):
    """还没开始处理模板就退出（配置非法 / 缺依赖）：在状态文件里记一笔，保留上一轮的模板明细与退避记录。"""
    prev = load_state()
    prev.update(running=False, ok=False, finished_at=int(time.time()), message=message[:500])
    save_state(prev)


# ---------------------------------------------------------------- 主流程

def list_mode(node: str, as_json: bool):
    inv = node_inventory(node)
    rows = []
    for vm in inv:
        meta = vm['meta'] or {}
        rows.append({
            'vmid': vm['vmid'], 'name': vm['name'], 'managed': bool(vm['meta']), 'retired': vm['retired'],
            'built_at': meta.get('built_at'), 'source': (meta.get('source') or {}).get('url'),
            'tool': meta.get('tool'), 'linked_clones': len(pitlib.linked_clone_refs(vm['vmid'])),
        })
    if as_json:
        print(json.dumps({'node': node, 'templates': rows}, indent=1, sort_keys=True))
        return
    print('%-6s %-40s %-8s %-17s %s' % ('VMID', 'NAME', 'STATE', 'BUILT', 'SOURCE'))
    for r in rows:
        state = 'retired' if r['retired'] else ('managed' if r['managed'] else 'legacy')
        built = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['built_at'])) if r['built_at'] else '-'
        src = (r['source'] or '-').rsplit('/', 1)[-1]
        clones = '  (%d linked clones)' % r['linked_clones'] if r['linked_clones'] else ''
        print('%-6d %-40s %-8s %-17s %s%s' % (r['vmid'], r['name'], state, built, src, clones))


def select_templates(imp, config: dict, selector: str) -> list:
    all_tpls = config.get('templates', []) or []
    if not selector:
        return list(all_tpls)
    return imp.match_templates(all_tpls, selector)


def run_sync(args, conf: dict) -> dict:
    imp = load_import_module()
    if args.insecure:
        imp.disable_tls_verification()
    config = imp.load_config()
    mirrors = config.get('mirrors', {}) or {}
    mirror_name = args.mirror if args.mirror is not None else (conf['MIRROR'] or None)
    if mirror_name and mirror_name not in mirrors:
        raise pitlib.PitError('mirror %r is not defined in templates.yaml' % mirror_name)
    mirror_config = mirrors.get(mirror_name) if mirror_name else None

    opts = {
        'upgrade': not args.no_upgrade and conf['UPGRADE'] == '1',
        'tuning': not args.no_tuning and conf['NET_TUNING'] == '1',
        'tcg': args.tcg,
        'keep_cache': conf['KEEP_CACHE'] == '1',
        'smoke_timeout': int(conf['SMOKE_TIMEOUT']) if conf['SMOKE_TEST'] == '1' else None,
    }
    max_builds = args.max_builds if args.max_builds is not None else int(conf['MAX_BUILDS'])
    max_age = args.max_age_days if args.max_age_days is not None else int(conf['MAX_AGE_DAYS'])
    keep = args.keep if args.keep is not None else int(conf['KEEP_RETIRED'])
    add_missing = args.add_missing or conf['ADD_MISSING'] == '1'
    new_storage = args.storage or conf['STORAGE'] or None
    selector = ','.join(args.selectors) or conf['SELECTOR'] or ('@' + conf['PROFILE'] if conf['PROFILE'] else '')
    # 补缺必须有明确的范围：没选择器时只补 templates.yaml 的 default_profile（否则空配置 + ADD_MISSING 会把 27 个模板
    # 连 EOL 的一起装进小硬盘）；EOL 的永远不补
    missing_selector = selector or ('@' + str(config.get('default_profile') or 'standard'))
    adopt_legacy = conf['ADOPT_LEGACY'] == '1'
    backoff = int(conf['BACKOFF_HOURS']) * 3600

    node = pitlib.local_node_name()
    if not node:
        raise pitlib.PitError('cannot determine the local PVE node name')

    now = time.time()
    previous = load_state()
    failures = previous.get('failures') if isinstance(previous.get('failures'), dict) else {}
    global _REPORT
    report = _REPORT = {
        'tool': 'pve-import-template', 'version': imp.TOOL_VERSION, 'node': node, 'dry_run': args.dry_run,
        'started_at': int(now), 'finished_at': None, 'running': True, 'pid': os.getpid(),
        'selector': selector, 'templates': [], 'failures': failures,
        'vmid_high_water': int(previous.get('vmid_high_water', 0) or 0),
    }
    if not args.dry_run:
        save_state(dict(report))
        cleanup_stale_builds(node, conf, args.dry_run)

    selected = select_templates(imp, config, selector)
    if selector and not selected:
        raise pitlib.PitError('selector %r matches no template in templates.yaml' % selector)
    addable = {t['name'] for t in select_templates(imp, config, missing_selector) if not pitlib.is_eol(t)} if add_missing else set()

    inventory = node_inventory(node)
    used = pitlib.cluster_used_vmids()
    rows, candidates = [], []
    for tpl in selected:
        name = tpl['name']
        # 只管平台认得的区间（9001-9999）：区间外的同名模板多半是运维手工做的，不重建、不退役、不回收
        actives = [vm for vm in inventory if not vm['retired'] and vm['name'].lower() == name.lower() and in_range(vm['vmid'], conf)]
        row = {'name': name, 'action': None, 'reason': None, 'detail': None, 'old_vmids': [v['vmid'] for v in actives]}
        rows.append(row)
        recipe = imp.recipe_of(tpl, opts['tuning'], opts['upgrade'], mirror_name)
        if not actives:
            if name not in addable:
                why = 'end-of-life' if add_missing and pitlib.is_eol(tpl) else 'not on this node (use --add-missing to import it)'
                row.update(action='absent', detail=why)
                continue
            if not new_storage:
                row.update(action='failed', detail='no storage configured for new templates (--storage / STORAGE)')
                continue
            if not args.force and (wait := backoff_remaining(failures.get(name), tpl, recipe, backoff, now)) > 0:
                row.update(action='backoff', reason='missing', detail='import failed before; retry in %d h: %s'
                           % (wait // 3600 + 1, (failures[name] or {}).get('error', '?')))
                continue
            candidates.append((row, tpl, actives, 'missing', 'not on this node', None))
            continue
        try:
            reason, detail, url, warnings = decide(tpl, current_of(actives), imp=imp, force=args.force,
                                                   max_age_days=0 if pitlib.is_eol(tpl) else max_age, now=now,
                                                   recipe=recipe, config=config)
        except Exception as e:  # noqa: BLE001 - 一个模板探测出错不影响其它模板
            row.update(action='failed', detail='probe failed: %s' % e)
            continue
        if warnings:
            row['warnings'] = warnings
        if reason == 'legacy' and not adopt_legacy:
            row.update(action='legacy', detail='imported by an older tool; ADOPT_LEGACY=0 keeps it as is')
            continue
        if reason is None:
            row.update(action='current', detail=detail)
            # 上一轮改名退役撞上克隆锁留下的同名多个在役：只留当前那个，其余补退役
            if len(actives) > 1:
                current = current_of(actives)
                others = [old for old in actives if old['vmid'] != current['vmid']]
                if args.dry_run:
                    row['would_retire'] = [old['vmid'] for old in others]
                else:
                    extra = [old['vmid'] for old in others if retire(old, name, row.setdefault('warnings', []))]
                    if extra:
                        row['retired'] = extra
            continue
        wait = backoff_remaining(failures.get(name), tpl, recipe, backoff, now)
        if not args.force and wait > 0:
            last_fail = failures[name]
            row.update(action='backoff', reason=reason,
                       detail='last attempt failed (%s, %d×); retry in %d h: %s'
                              % (last_fail.get('kind', 'error'), last_fail.get('count', 1), wait // 3600 + 1,
                                 last_fail.get('error', '?')))
            continue
        candidates.append((row, tpl, actives, reason, detail, url))

    candidates.sort(key=lambda c: (REASON_PRIORITY.get(c[3], 9), pitlib.is_eol(c[1]),
                                   (current_of(c[2])['meta'] or {}).get('built_at', 0) if c[2] else 0))
    builds = 0
    for row, tpl, actives, reason, detail, url in candidates:
        row.update(reason=reason, detail=detail)
        # 不能建的在规划时就判掉，不占本轮名额（旧版先占名额再在构建里抛 Skip：排在前面的 OVMF 模板每轮都挤掉别的）
        current = current_of(actives) if actives else None
        if current is not None and str(current['config'].get('bios', 'seabios')) != 'seabios':
            row.update(action='skipped', detail='%s (VM %d) uses %s firmware; only SeaBIOS templates are rebuilt automatically'
                       % (tpl['name'], current['vmid'], current['config'].get('bios')))
            continue
        storage = new_storage
        volid = ''
        if current is not None:
            volid = pitlib.find_os_disk_volid(current['config']) or ''
            storage = volid.split(':', 1)[0] if ':' in volid else new_storage
        if not storage:
            row.update(action='failed', detail='cannot determine the storage of the current template')
            continue
        if max_builds and builds >= max_builds:
            row['action'] = 'deferred'
            continue
        builds += 1
        if args.dry_run:
            row['action'] = 'would-build'
            continue
        name = tpl['name']
        started = time.time()
        report['current'] = name
        save_state(dict(report, templates=rows))
        recipe = imp.recipe_of(tpl, opts['tuning'], opts['upgrade'], mirror_name)
        try:
            if url is None:
                url = pitlib.resolve_source_url(tpl, log=log, config=config)
            result = build_one(imp, tpl, node, actives, storage=storage, used=used, conf=conf, opts=opts,
                               mirror_config=mirror_config, mirror_name=mirror_name, url=url, state=report,
                               config=config, inventory=inventory, current_volid=volid)
            row.update(action='built', new_vmid=result['new_vmid'], retired=result['retired'],
                       built_at=result['meta']['built_at'], source=result['meta']['source'].get('url'),
                       checksum_verified=result['meta']['checksum_verified'], warnings=result['warnings'])
            failures.pop(name, None)
            save_state(dict(report, templates=rows))  # 高水位立即落盘：后面的模板失败 / 被杀也不会回头复用号
        except Skip as e:
            row.update(action='skipped', detail=str(e)[:500])
        except KeyboardInterrupt:
            row.update(action='failed', detail='interrupted')
            raise
        except Exception as e:  # noqa: BLE001 - 单模板失败隔离；appliance 起不来照样只记这一个
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            kind = failure_kind(e)
            row.update(action='failed', detail=msg[:500], failure=kind)
            prev = failures.get(name) or {}
            failures[name] = {'at': int(time.time()), 'recipe': recipe, 'url': source_key(tpl), 'kind': kind,
                              'count': int(prev.get('count', 0)) + 1 if prev.get('recipe') == recipe else 1,
                              'error': msg[:300]}
            log('FAILED %s (%s): %s' % (name, kind, msg))
            if isinstance(e, pitlib.LaunchError):
                log('libguestfs appliance cannot start on this host (even with TCG); run setup.sh --full-check')
        row['duration_s'] = int(time.time() - started)

    # 回收：只动本次处理过的模板名（不碰没选中的）
    for row in rows:
        if row['action'] in ('built', 'current'):
            deleted, notes = collect_garbage(node, row['name'], keep, time.time(), args.dry_run)
            if deleted:
                row['deleted'] = deleted
            if notes:
                row['gc_notes'] = notes

    summary = {}
    for row in rows:
        summary[row['action']] = summary.get(row['action'], 0) + 1
    report.update(templates=rows, summary=summary, failures=failures, running=False,
                  finished_at=int(time.time()), ok=summary.get('failed', 0) == 0)
    report.pop('current', None)
    return report


def print_human(report: dict):
    print('\n===== sync-templates on %s: %s =====' % (report['node'], ', '.join(
        '%s %d' % (k, v) for k, v in sorted(report.get('summary', {}).items()))))
    for row in report['templates']:
        extra = ''
        if row.get('new_vmid'):
            extra = ' → VM %d (retired %s)' % (row['new_vmid'], ','.join(map(str, row.get('retired', []))) or '-')
        if row.get('deleted'):
            extra += ' deleted %s' % ','.join(map(str, row['deleted']))
        print('  %-12s %-24s %s%s' % (row['action'], row['name'], row.get('detail') or '', extra))


def main(argv) -> int:
    args = parse_args(argv)
    if args.help:
        print(__doc__)
        return 0
    real_run = not (args.list or args.dry_run)
    for tool in ('pvesh', 'qm') + (('virt-customize', 'qemu-img') if real_run else ()):
        if shutil.which(tool) is None:
            print('Missing dependency: %s (run setup.sh)' % tool, file=sys.stderr)
            if real_run:
                record_failure('missing dependency: %s (run setup.sh)' % tool)
            return 2
    try:
        conf = load_conf(args.config or DEFAULT_CONF, explicit=args.config is not None)
    except pitlib.PitError as e:
        # 配置坏了也要留痕：否则 last-run.json 一直是上一次的 OK，平台看着「正常」，模板却几个月没更新
        if real_run:
            record_failure('config error: %s' % e)
        raise

    if args.list:
        node = pitlib.local_node_name()
        if not node:
            raise pitlib.PitError('cannot determine the local PVE node name')
        list_mode(node, args.json)
        return 0

    if args.dry_run:
        report = run_sync(args, conf)
    else:
        with pitlib.ToolLock():
            log_path = open_log()
            log('sync-templates started (log %s)' % log_path)
            try:
                report = run_sync(args, conf)
            except BaseException as e:
                doc = dict(_REPORT) if _REPORT is not None else load_state()
                doc.update(running=False, finished_at=int(time.time()), ok=False,
                           message=('interrupted' if isinstance(e, KeyboardInterrupt) else str(e))[:500])
                doc.pop('current', None)
                save_state(doc)
                raise
            report['log'] = log_path
            save_state(report)

    if args.json:
        print(json.dumps(report, sort_keys=True))
    else:
        print_human(report)
    return 0 if report.get('ok', True) else 1


if __name__ == '__main__':
    pitlib.raise_on_sigterm()
    try:
        sys.exit(main(sys.argv[1:]))
    except pitlib.LockBusy as exc:
        print('Error: %s' % exc, file=sys.stderr)
        sys.exit(3)
    except pitlib.PitError as exc:
        print('Error: %s' % exc, file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        sys.exit(130)
