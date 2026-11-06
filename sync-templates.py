#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync-templates.py —— 让本节点的模板保持最新：新建 → 切换 → 回收（蓝绿），非交互，可 JSON 输出。

为什么不就地改模板（update-templates.py）：模板基卷被链接克隆共享（qcow2 backing / ZFS 快照），
就地写要么被 PVE 设为只读拒写、要么改了也不进新克隆、要么改坏已有克隆。这里每次都**新建**一个模板，
构建完全成功后才接管名字，旧模板改名为 <name>-retired-<时间> 并打 pit-retired 标记，过了宽限期、
确认没有链接克隆引用后再删。任何一步失败，旧模板都原样在役。

什么时候重建一个模板（按优先级）：
  forced          显式 --force（被名额 / 时间预算 / 内存推迟的记 pending_forced，之后的运行接着当 forced 建）
  source-changed  上游镜像变了（discover 找到新点版本 / 校验和变了 / ETag 变化）；首次见到后满 SOURCE_DELAY_HOURS 才采用
  stale           距上次构建超过 MAX_AGE_DAYS（镜像没变也要重新跑包升级拿安全补丁）
  recipe-changed  注入方式变了（templates.yaml 条目 / uploads 脚本 / 构建流程版本 / 开关）
  legacy          旧工具导入的模板，没有构建元数据
  missing         本节点还没有（仅 --add-missing）
被 --rollback 回滚过的模板处于 hold：以上自动原因全部跳过，上游出了新镜像（源标识变了）才自动解除，或 --release 手动解除。

用法
  python3 sync-templates.py [选择器 ...] [选项]
  python3 sync-templates.py --rollback NAME      把 NAME 换回最近一个退役版本，并 hold 住
  python3 sync-templates.py --release NAME       解除 NAME 的 hold

  选择器：模板名 / glob / 系列名（同 import.py），大小写不敏感；不写 = 配置里的 SELECTOR，再不写 = 全部
  默认只处理本节点**已有**的模板；--add-missing 才会导入缺的（需要 --storage 或配置 STORAGE）。

选项
  --config <path>     配置文件（默认 /etc/pve-import-template.conf，不存在则用内置默认值）
  --force             不看是否需要，选中的全部重建（不越过 hold）
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
  --rollback <name>   回滚：恢复区间内最新的未锁退役版本，当前在役的按标准流程退役，写 hold
                      （不能与选择器 / --force / --add-missing 同用）
  --release <name>    删除 hold
  --dry-run           只打印计划（会探测上游，不构建、不改任何东西）
  --json              stdout 只输出一份 JSON 报告（日志走 stderr）
  --list              列出本节点模板及其构建信息后退出（配合 --json 输出 JSON）
  -h, --help

退出码：0 全部成功 / 1 有模板失败（或回滚失败）/ 2 用法或依赖错误 / 3 另一个实例在跑 /
        4 集群没有 quorum（跳过本轮）/ 130 被中断
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
    # 冒烟加深：qga 应答后再等 cloud-init 跑完、查 SSH host key / sshd -t / sshd 在跑（同一个 SMOKE_TIMEOUT 预算）
    'SMOKE_DEEP': (r'[01]', '1'),
    # 1 = 整个节点暂停自动维护：sync 拿锁后什么都不做（退出码 0，状态写 paused）。--rollback / --release / --list 照常
    'PAUSED': (r'[01]', '0'),
    # 上游新镜像第一次见到后满 N 小时才采用（金丝雀节点设 0 先吃；其余节点等一天，坏镜像有时间被发现、hold 住）
    'SOURCE_DELAY_HOURS': (r'[0-9]{1,3}', '24'),
    # 每个模板构建前 / 冒烟前的 MemAvailable 下限：不够记 deferred（不算失败、不进退避）
    'MIN_FREE_MEM_MB': (r'[0-9]{1,6}', '4096'),
    # 本轮开始超过这么久就不再开新构建（剩下的 deferred）；0 = 不限。systemd 的 TimeoutStartSec 是 6h
    'RUN_BUDGET_HOURS': (r'[0-9]{1,2}', '4'),
    # 下载缓存目录（空 = 环境变量 CLOUD_IMG_DIR，再不设 = 脚本目录下的 cloud_img）
    'CACHE_DIR': (r'(|/[A-Za-z0-9._/-]{1,200})', ''),
}

REASON_PRIORITY = {'forced': 0, 'source-changed': 1, 'stale': 2, 'recipe-changed': 3, 'legacy': 4, 'missing': 5}
# 上游 404（镜像被删 / 改名）不是隔天就会好的事：等 templates.yaml 更新（url 变了就不再退避）
NOT_FOUND_BACKOFF = 7 * 86400
MAX_BACKOFF = 7 * 86400
# 被中断（timer 超时 / 关机 / Ctrl-C）的模板：不是它自己的错，2 小时后就可以再试
INTERRUPTED_BACKOFF = 2 * 3600
# 结果行的 action 词表（summary 里每个都有计数，没有的记 0）
ACTIONS = ('built', 'current', 'failed', 'deferred', 'backoff', 'absent', 'held', 'waiting', 'skipped',
           'would-build', 'would-retire', 'would-delete', 'rolled-back')
NAME_RE = r'[A-Za-z0-9][A-Za-z0-9.-]{0,40}'

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


def tool_version() -> str:
    """不加载 import.py（要 PyYAML）也能拿到版本号：暂停时的状态文件也要写版本。"""
    with contextlib.suppress(OSError):
        with open(os.path.join(SCRIPT_DIR, 'VERSION')) as fh:
            return fh.read().strip()
    return '?'


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
    p.add_argument('--rollback', metavar='NAME')
    p.add_argument('--release', metavar='NAME')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--json', action='store_true')
    p.add_argument('--list', action='store_true')
    p.add_argument('-h', '--help', action='store_true')
    args = p.parse_args(argv)
    # agent 习惯在选择器前加 `--`：不同 Python 版本对结尾单独的 `--` 处理不一，统一丢掉（之后的仍按选择器校验）
    args.selectors = [s for s in args.selectors if s != '--']
    for name in ('max_builds', 'max_age_days', 'keep'):
        v = getattr(args, name)
        if v is not None and v < 0:
            p.error('--%s must be >= 0' % name.replace('_', '-'))
    if args.storage and not re.fullmatch(CONF_SCHEMA['STORAGE'][0], args.storage):
        p.error('--storage has an invalid value')
    for sel in args.selectors:
        if not re.fullmatch(r'[A-Za-z0-9@*?][A-Za-z0-9.*?,_@-]{0,199}', sel):
            p.error('invalid selector %r' % sel)
    for opt in ('rollback', 'release'):
        value = getattr(args, opt)
        if value is not None and not re.fullmatch(NAME_RE, value):
            p.error('--%s needs a template name' % opt)
    if args.rollback and args.release:
        p.error('--rollback and --release cannot be used together')
    for opt in ('rollback', 'release'):
        if getattr(args, opt) and (args.selectors or args.force or args.add_missing):
            p.error('--%s cannot be combined with selectors, --force or --add-missing' % opt)
        if getattr(args, opt) and (args.dry_run or args.list):
            p.error('--%s cannot be combined with --dry-run or --list' % opt)
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


def cache_dir_of(conf: dict) -> str:
    return conf.get('CACHE_DIR') or os.environ.get('CLOUD_IMG_DIR') or os.path.join(SCRIPT_DIR, 'cloud_img')


def destroy_ours(vmid: int, skiplock: bool = False) -> bool:
    """删掉本工具这次 / 上次建的东西（调用方已核对过）；失败不抛异常。"""
    try:
        pitlib.run_cmd(['qm', 'destroy', vmid, '--purge', '1'] + (['--skiplock', '1'] if skiplock else []),
                       timeout=600, log=log)
        return True
    except pitlib.PitError:
        return False


def cleanup_stale_builds(node: str, conf: dict, dry_run: bool, cache_dir: Optional[str] = None) -> list:
    """上次被强杀留下的半成品，逐条核对是本工具的东西才删（客户 VM 名是 VM-<id>，但防线不能只靠名字）。返回 [(模板名, 备注)]。

    - 构建中的 VM / 已转模板但没来得及改名的 `<name>-pitbuild`：区间内、带 pit tag、description 的 pit-meta 名字对得上；
    - 开机冒烟留下的 `pitsmoke-<vmid>`：带 pit-smoke tag（可能还开着，先停）；
    - 被强杀时它们常带着 clone / create 锁（克隆 / 导入进行到一半）：核对无误后 --skiplock 删；别的锁（backup 等）不动，记备注；
    - 镜像缓存里的工作副本与下载到一半的 .img.download（每份几 GB）。
    """
    notes = []
    # 冒烟克隆先删（它是 -pitbuild 模板的链接克隆，不先删它模板删不掉）
    vms = sorted(pitlib.node_vms(node), key=lambda v: (not v['name'].startswith('pitsmoke-'), v['vmid']))
    names_by_id = {vm['vmid']: vm['name'] for vm in vms}
    for vm in vms:
        if not in_range(vm['vmid'], conf):
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
            owner = str(meta['name'])
        else:
            src = re.fullmatch(r'pitsmoke-(\d+)', vm['name'])
            src_name = names_by_id.get(int(src.group(1))) if src else None
            owner = src_name[:-len(BUILD_SUFFIX)] if src_name and src_name.endswith(BUILD_SUFFIX) else src_name
        kind = 'smoke-test' if smoke else 'build'
        if vm['lock'] and vm['lock'] not in ('clone', 'create'):
            notes.append((owner, 'leftover %s VM %d (%s) is locked (%s); left alone'
                          % (kind, vm['vmid'], vm['name'], vm['lock'])))
            continue
        log('removing leftover %s VM %d (%s)%s' % (kind, vm['vmid'], vm['name'],
                                                    ' with lock %s' % vm['lock'] if vm['lock'] else ''))
        if dry_run:
            continue
        skiplock = bool(vm['lock'])
        if vm['status'] == 'running':
            with contextlib.suppress(pitlib.PitError):
                pitlib.run_cmd(['qm', 'stop', vm['vmid'], '--timeout', '30'] + (['--skiplock', '1'] if skiplock else []),
                               timeout=120, log=log)
        if not destroy_ours(vm['vmid'], skiplock):
            notes.append((owner, 'could not remove leftover %s VM %d (%s)' % (kind, vm['vmid'], vm['name'])))
    cache = cache_dir or cache_dir_of(conf)
    if not dry_run:
        work = os.path.join(cache, 'work')
        if os.path.isdir(work):
            for f in os.listdir(work):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(work, f))
        with contextlib.suppress(OSError):
            for f in os.listdir(cache):
                if f.endswith('.img.download'):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(cache, f))
    return notes


def current_of(actives: list) -> dict:
    """同名在役模板里「当前」那个：构建时间最新，其次 VMID 最大。"""
    return sorted(actives, key=lambda v: ((v['meta'] or {}).get('built_at', 0), v['vmid']), reverse=True)[0]


retired_at = pitlib.retired_at


def source_name(url) -> Optional[str]:
    return str(url).rsplit('/', 1)[-1] if url else None


def fmt_time(ts) -> str:
    try:
        return time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return '?'


def fmt_age(seconds) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return '%dd%dh' % (seconds // 86400, seconds % 86400 // 3600)
    if seconds >= 3600:
        return '%dh%dm' % (seconds // 3600, seconds % 3600 // 60)
    return '%dm' % (seconds // 60)


def state_dict(doc: dict, key: str) -> dict:
    """旧状态文件可能没有这个字段（或被人改坏）：一律按空处理。"""
    value = doc.get(key)
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- 决策

def failure_kind(exc: BaseException) -> str:
    kind = getattr(exc, 'kind', None)
    if kind:
        return str(kind)
    if isinstance(exc, pitlib.LaunchError):
        return 'appliance'
    if 'insufficient space' in str(exc) or 'not enough space' in str(exc):
        return 'space'
    if 'smoke test failed' in str(exc):
        return 'smoke'
    return 'build'


def backoff_remaining(last_fail: dict, tpl: dict, recipe: str, base: int, now: float) -> int:
    """上次失败后还要等多少秒。配方或 yaml 里的 url / discover 变了（有人修过了）立即重试；
    404 固定退避一周；被中断的固定 2 小时；其它按连续失败次数翻倍（base, 2×base, …，封顶一周）。"""
    if not last_fail or last_fail.get('recipe') != recipe or last_fail.get('url') != source_key(tpl):
        return 0
    if last_fail.get('kind') == 'not-found':
        window = NOT_FOUND_BACKOFF
    elif last_fail.get('kind') == 'interrupted':
        window = INTERRUPTED_BACKOFF
    else:
        window = min(base * (2 ** max(0, int(last_fail.get('count', 1)) - 1)), MAX_BACKOFF)
    return max(0, int(float(last_fail.get('at', 0)) + window - now))


def source_key(tpl: dict) -> str:
    """yaml 里决定「去哪下载」的部分（url + discover），改了就该立即重试。"""
    return json.dumps([tpl.get('url'), tpl.get('discover')], sort_keys=True)


def decide(tpl: dict, current: dict, *, imp, force: bool, max_age_days: int, now: float, recipe: str,
           config: dict, checksum_of=None) -> dict:
    """要不要重建：{reason, detail, url, how, warnings, source_id}；reason=None 表示不需要。只做只读探测（目录索引 / 校验文件 / HEAD）。

    url 是构建要用的下载地址——决策定了就传给构建，不让构建再解析一次（两次之间索引可能变了）。
    how：resolve_source 的结果（pinned / discovered / fallback）。source_id：source-changed 时新上游的标识
    （新文件名 / 新校验和 / 新 ETag），SOURCE_DELAY_HOURS 按它记「第一次见到」。
    """
    meta = current.get('meta') or {}
    src = meta.get('source') or {}
    checksum_of = checksum_of or (lambda t, u: imp.upstream_checksum(t, u, config))
    warnings = []
    url, how = pitlib.resolve_source(tpl, log=log, config=config, last_known=src.get('url'))
    out = {'reason': None, 'detail': 'up to date', 'url': url, 'how': how, 'warnings': warnings, 'source_id': None}
    if how == 'fallback':
        warnings.append('discover failed (upstream renamed its files?); kept %s' % source_name(url))
    if force:
        out.update(reason='forced', detail='requested')
        return out
    if not current.get('meta'):
        out.update(reason='legacy', detail='no build metadata (imported by an older tool)')
        return out
    if how != 'fallback' and url != src.get('url'):
        out.update(reason='source-changed', detail='new upstream image %s' % source_name(url), source_id=source_name(url))
        return out
    if max_age_days > 0 and now - float(meta.get('built_at', 0)) > max_age_days * 86400:
        out.update(reason='stale', detail='built %d days ago' % ((now - float(meta.get('built_at', 0))) // 86400))
        return out
    if meta.get('recipe') != recipe:
        out.update(reason='recipe-changed', detail='injection recipe changed')
        return out
    # 同一个地址重新发布：有校验文件就比上游 hash（HEAD 会被 Fedora / openSUSE / Arch 重定向到不同镜像站，
    # ETag 每次都不一样，按 ETag 判会天天重建）；没有校验文件才退回 ETag / Last-Modified
    if src.get('checksum') and (tpl.get('checksum') or tpl.get('checksum_pattern')):
        upstream = checksum_of(tpl, url)
        if upstream and upstream != src['checksum']:
            out.update(reason='source-changed', detail='upstream image was republished (checksum changed)',
                       source_id=upstream)
        return out
    try:
        head = pitlib.http_head(url)
        if (head.get('etag') or head.get('last_modified')) and not imp.source_unchanged(src, head):
            out.update(reason='source-changed', detail='upstream image was republished',
                       source_id=head.get('etag') or head.get('last_modified'))
    except Exception as e:  # noqa: BLE001 - 探测失败不判「变了」，只告警
        log('Warning: HEAD %s failed (%s); assuming unchanged' % (url, e))
    return out


def hold_released(hold: dict, tpl: dict, url: str, how: str, checksum_of) -> bool:
    """hold 能不能自动解除：上游的源标识跟回滚掉的那个坏构建不一样了（新文件名，或同一地址校验和变了）。

    目录索引打不开（fallback）时 url 是在役模板的旧地址，跟坏构建当然不一样——这种「不一样」不算数。
    坏构建的来源记不全（没有 bad_url）就一直 hold，等人 --release。
    """
    if how == 'fallback':
        return False
    bad_url = hold.get('bad_url')
    if not bad_url:
        return False
    if source_name(url) != source_name(bad_url):
        return True
    bad_sum = hold.get('bad_checksum')
    if bad_sum and (tpl.get('checksum') or tpl.get('checksum_pattern')):
        upstream = checksum_of(tpl, url)
        return bool(upstream and upstream != bad_sum)
    return False


def held_detail(hold: dict, forced: bool) -> str:
    detail = 'held since %s (%s' % (fmt_time(hold.get('at')), hold.get('by') or 'manual')
    if hold.get('bad_vmid'):
        detail += ' of VM %s' % hold['bad_vmid']
    detail += '); waits for a new upstream image'
    if forced:
        detail += '; --force does not override a hold, run --release first'
    else:
        detail += ' or --release'
    return detail


class Skip(pitlib.PitError):
    """不能（也不该）自动重建的模板：记 skipped，不算失败、不进退避。"""


class RollbackError(pitlib.PitError):
    """回滚做不了（没有可恢复的退役版本 / 退役当前模板失败）：退出码 1。"""


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
    warnings = []
    current = current_of(actives) if actives else None
    old_hook = ''
    if current is not None:
        cfg = current['config']
        if str(cfg.get('bios', 'seabios')) != 'seabios':
            raise Skip('%s (VM %d) uses %s firmware; only SeaBIOS templates are rebuilt automatically'
                       % (name, current['vmid'], cfg.get('bios')))
        # hookscript 取旧模板的**原始**值核对（不经 carry_over 的正则过滤）：格式不认识就别建——新模板会丢 hook，
        # 客户 VM 克隆出来没有 VRF 路由 / BCP38
        old_hook = str(cfg.get('hookscript') or '').strip()
        if old_hook and not re.fullmatch(pitlib.CARRY_OVER_KEYS['hookscript'], old_hook):
            raise pitlib.PitError('%s (VM %d) has hookscript %r in an unexpected format; not rebuilding it '
                                  '(the new template would lose the hook)' % (name, current['vmid'], old_hook))
        # 节点策略键（hookscript / vga / machine / cicustom …）照抄旧模板
        extra_config = pitlib.carry_over(cfg)
        bridge = pitlib.net_bridge(cfg)
        if bridge:
            vm_opts['bridge'] = bridge
        for key in ('memory', 'cores'):
            if str(cfg.get(key, '')).isdigit():
                vm_opts[key] = int(cfg[key])
        if cfg.get('cpu'):
            vm_opts['cpu'] = str(cfg['cpu'])
            level = tpl.get('min_cpu_level')
            if level and not pitlib.cpu_model_ok(vm_opts['cpu'], int(level)):
                # EL10 要 x86-64-v3：kvm64 / x86-64-v2-AES 这类 CPU 型号下内核起不来
                warnings.append('cpu %s cannot run x86-64-v%d guests; the new template uses host' % (vm_opts['cpu'], int(level)))
                vm_opts['cpu'] = 'host'
    else:
        # 新导入的取本节点模板最常见的 hookscript
        extra_config = node_hookscript(inventory or [], conf)
        if conf.get('BRIDGE'):
            vm_opts['bridge'] = conf['BRIDGE']

    lo, hi = int(conf['VMID_MIN']), int(conf['VMID_MAX'])

    def allocate() -> int:
        # qm create 前一刻才分配（构建要跑很久，集群里别的节点可能刚占了号）；单调往上，不复用回收掉的号。
        # 游标 = max(本节点状态, 集群共享文件)：别的节点刚分配过的号段这里也跳过
        used.update(pitlib.cluster_used_vmids())
        hw = pitlib.combined_high_water(int(state.get('vmid_high_water', 0) or 0), lo, hi)
        vid = pitlib.allocate_vmid(used, lo, hi, hw)
        used.add(vid)
        # 游标记「刚分配的号」而不是 max：转满一圈回绕后游标跟着回到低位（记 max 的话此后每次都从顶端回绕 = 取最小空闲号）
        state['vmid_high_water'] = vid
        pitlib.write_shared_high_water(vid)
        return vid

    def allocate_smoke() -> int:
        # 冒烟临时 VM 用完即删：从区间顶端往下取，不推高模板游标
        used.update(pitlib.cluster_used_vmids())
        vid = pitlib.allocate_vmid_top(used, lo, hi)
        used.add(vid)
        return vid

    expected = imp.upstream_checksum(tpl, url, config) if (tpl.get('checksum') or tpl.get('checksum_pattern')) else None
    log('building %s on storage %s from %s' % (name, storage, source_name(url)))
    meta = imp.build_template(tpl, storage, allocate, vm_name=name + BUILD_SUFFIX, mirror_config=mirror_config,
                              mirror_name=mirror_name, apply_tuning=opts['tuning'], upgrade=opts['upgrade'],
                              vm_opts=vm_opts, force_tcg=opts['tcg'], keep_cache=opts['keep_cache'],
                              node=node, log=log, source_url=url, smoke_timeout=opts['smoke_timeout'],
                              smoke_allocate=allocate_smoke, extra_config=extra_config,
                              disk_format=pitlib.disk_format_for(storage, current_volid or None),
                              expected_checksum=expected, require_checksum=True,
                              smoke_deep=opts.get('smoke_deep', False), min_free_mem_mb=opts.get('min_mem', 0))
    vmid = int(meta['vmid'])
    warnings.extend(meta.get('warnings') or [])
    try:
        # 切换前看新模板**实际**的配置：旧模板有 hookscript、新的没有或不同就不切（客户 VM 会没有 hook）
        new_cfg = pitlib.vm_config(node, vmid)
        new_hook = str(new_cfg.get('hookscript') or '').strip()
        if old_hook and new_hook != old_hook:
            raise pitlib.PitError('new template VM %d has hookscript %r but the one it replaces has %r; not switching'
                                  % (vmid, new_hook or None, old_hook))
        if current is not None:
            missing = pitlib.uncarried_keys(current['config'], new_cfg)
            if missing:
                warnings.append('not carried over from VM %d: %s' % (current['vmid'], ', '.join(missing)))
        # 切换：先让新模板接管名字，再把旧的改名退役——两步之间两个同名模板都在，克隆哪个都能用，没有空窗
        pitlib.run_cmd(['qm', 'set', vmid, '--name', name], timeout=120, log=log)
    except BaseException:
        destroy_ours(vmid, skiplock=True)   # 还叫 <name>-pitbuild、是这次建的：删掉，不留给下一轮清理
        raise
    retired = []
    for old in actives:
        if retire(old, name, warnings):
            retired.append(old['vmid'])
    return {'action': 'built', 'new_vmid': vmid, 'retired': retired, 'meta': meta, 'warnings': warnings}


def collect_garbage(node: str, name: str, keep: int, now: float, dry_run: bool, vmid_range: Optional[tuple] = None,
                    vms: Optional[list] = None) -> tuple:
    """(deleted, kept_notes)：同名 retired 模板保留最新 keep 个，其余过了宽限期、没有链接克隆、没保护的删除。
    只认本工具退役的（pit-retired + 名字里能解析出退役时间），运维手工起名的 *-retired-old 永远不动。
    vmid_range=(lo, hi)：只看区间内的。dry_run 时 deleted 是「会删的」。"""
    retired = [vm for vm in (vms if vms is not None else pitlib.node_vms(node))
               if vm['template'] and pitlib.retired_of(vm['name'], name)
               and (vmid_range is None or vmid_range[0] <= vm['vmid'] <= vmid_range[1])]
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
        log('%s retired template %d (%s)' % ('would delete' if dry_run else 'deleting', vm['vmid'], vm['name']))
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
    """还没开始处理模板就退出（配置非法 / 缺依赖 / 没 quorum）：在状态文件里记一笔，保留上一轮的模板明细与退避记录。"""
    prev = load_state()
    prev.update(running=False, ok=False, finished_at=int(time.time()), message=message[:500])
    prev.pop('current', None)
    save_state(prev)


def summarize(rows: list) -> dict:
    summary = {a: 0 for a in ACTIONS}
    for row in rows:
        if row.get('action'):
            summary[row['action']] = summary.get(row['action'], 0) + 1
    return summary


def mark_pending(pending: dict, name: str, now: float):
    """被推迟 / 中断的强制重建记下来，之后不带 --force 的运行接着当 forced 建（保留最早的请求时间）。"""
    entry = pending.get(name)
    if not isinstance(entry, dict) or not entry.get('at'):
        pending[name] = {'at': int(now)}


def paused_report() -> dict:
    """PAUSED=1：什么都不做，只把「暂停中」写进状态文件；上一轮的退避 / hold / pending / 游标原样保留。"""
    now = int(time.time())
    doc = load_state()
    for key in ('failures', 'holds', 'pending_forced', 'sources_seen'):
        doc[key] = state_dict(doc, key)
    doc.setdefault('vmid_high_water', 0)
    doc.pop('current', None)
    doc.update(tool='pve-import-template', version=tool_version(), node=pitlib.local_node_name(), paused=True, ok=True,
               running=False, dry_run=False, started_at=now, finished_at=now, pid=os.getpid(),
               message='paused (PAUSED=1)')
    save_state(doc)
    return doc


# ---------------------------------------------------------------- --list

def retired_base(vm_name: str) -> str:
    return re.split(re.escape(pitlib.RETIRED_INFIX), vm_name, maxsplit=1, flags=re.I)[0]


def list_mode(node: str, conf: dict, as_json: bool):
    imp = load_import_module()
    config = imp.load_config()
    yaml_by_name = {t['name'].lower(): t for t in config.get('templates') or []}
    selector = conf['SELECTOR'] or ('@' + conf['PROFILE'] if conf['PROFILE'] else '')
    maintained = {t['name'].lower() for t in select_templates(imp, config, selector)}
    keep = int(conf['KEEP_RETIRED'])
    now = time.time()
    inv = node_inventory(node)
    by_base = {}
    for vm in inv:
        if vm['retired'] and in_range(vm['vmid'], conf):
            by_base.setdefault(retired_base(vm['name']).lower(), []).append(vm)
    beyond_keep = {vm['vmid'] for vms in by_base.values() for vm in pitlib.gc_candidates(vms, keep)}
    rows = []
    for vm in inv:
        meta = vm['meta'] or {}
        cfg = vm['config']
        ranged = in_range(vm['vmid'], conf)
        base = retired_base(vm['name']) if vm['retired'] else vm['name']
        tpl = yaml_by_name.get(base.lower())
        volid = pitlib.find_os_disk_volid(cfg) or ''
        protection = str(cfg.get('protection', '0')) in ('1', 'true')
        refs = pitlib.linked_clone_refs(vm['vmid'])
        ts = retired_at(vm['name']) if vm['retired'] else 0
        retired_for = int(now - ts) if ts else None
        reclaimable = None
        if vm['retired']:
            # 能否回收 = 下一轮 GC 会不会删它（本工具退役的、超出保留代数、过了宽限期、无链接克隆、没保护、没锁）
            reclaimable = bool(ranged and pitlib.TAG_RETIRED in vm['tags'] and ts and vm['vmid'] in beyond_keep
                               and retired_for >= RETIRE_GRACE_SECONDS and not refs and not protection and not vm['lock'])
        if not ranged:
            state = 'out-of-range'      # 区间外的不归本工具管（不是 legacy，不会被重建 / 退役 / 回收）
        elif vm['retired']:
            state = 'retired'
        else:
            state = 'managed' if vm['meta'] else 'legacy'
        rows.append({
            'vmid': vm['vmid'], 'name': vm['name'], 'state': state, 'managed': bool(vm['meta']), 'retired': vm['retired'],
            'in_range': ranged, 'in_yaml': tpl is not None, 'yaml_name': tpl['name'] if tpl else None,
            'eol': pitlib.is_eol(tpl) if tpl else None, 'maintained': bool(tpl) and base.lower() in maintained,
            'storage': volid.split(':', 1)[0] if ':' in volid else None,
            'hookscript': bool(str(cfg.get('hookscript') or '').strip()),
            'retired_for_s': retired_for, 'reclaimable': reclaimable, 'protection': protection, 'lock': vm['lock'] or None,
            'built_at': meta.get('built_at'), 'source': (meta.get('source') or {}).get('url'),
            'tool': meta.get('tool'), 'linked_clones': len(refs),
        })
    if as_json:
        print(json.dumps({'node': node, 'vmid_range': [int(conf['VMID_MIN']), int(conf['VMID_MAX'])],
                          'templates': rows}, indent=1, sort_keys=True))
        return
    print('VMID range %s-%s; maintained: %s' % (conf['VMID_MIN'], conf['VMID_MAX'], selector or 'all of templates.yaml'))
    print('%-6s %-40s %-12s %-17s %-12s %-4s %s' % ('VMID', 'NAME', 'STATE', 'BUILT', 'STORAGE', 'HOOK', 'SOURCE / FLAGS'))
    for r in rows:
        built = time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['built_at'])) if r['built_at'] else '-'
        flags = []
        if not r['in_yaml']:
            flags.append('not-in-yaml')
        else:
            if r['eol']:
                flags.append('eol')
            if not r['maintained']:
                flags.append('not-maintained')
        if r['retired_for_s'] is not None:
            flags.append('retired %s ago, %s' % (fmt_age(r['retired_for_s']), 'reclaimable' if r['reclaimable'] else 'kept'))
        if r['protection']:
            flags.append('protected')
        if r['lock']:
            flags.append('lock=%s' % r['lock'])
        if r['linked_clones']:
            flags.append('%d linked clones' % r['linked_clones'])
        src = (r['source'] or '-').rsplit('/', 1)[-1]
        print('%-6d %-40s %-12s %-17s %-12s %-4s %s%s' % (r['vmid'], r['name'], r['state'], built, r['storage'] or '-',
                                                         'yes' if r['hookscript'] else 'no', src,
                                                         ('  [' + ', '.join(flags) + ']') if flags else ''))


def select_templates(imp, config: dict, selector: str) -> list:
    all_tpls = config.get('templates', []) or []
    if not selector:
        return list(all_tpls)
    return imp.match_templates(all_tpls, selector)


# ---------------------------------------------------------------- 回滚 / 解除 hold

def canonical_name(config: dict, name: str) -> str:
    for tpl in config.get('templates', []) or []:
        if tpl['name'].lower() == name.lower():
            return tpl['name']
    raise pitlib.PitError('%s is not a template in templates.yaml' % name)


def rollback(args, conf: dict) -> dict:
    """把 NAME 换回本节点区间内最新的、未锁的退役版本；当前在役的按标准流程退役；写 hold。返回（合并后的）状态文件。"""
    imp = load_import_module()
    config = imp.load_config()
    name = canonical_name(config, args.rollback)
    node = pitlib.local_node_name()
    if not node:
        raise pitlib.PitError('cannot determine the local PVE node name')
    now = time.time()
    previous = load_state()
    holds = state_dict(previous, 'holds')
    inventory = node_inventory(node)
    pattern = re.compile(re.escape(name) + re.escape(pitlib.RETIRED_INFIX) + r'\d{12}', re.I)
    restorable = [vm for vm in inventory if in_range(vm['vmid'], conf) and pitlib.TAG_RETIRED in vm['tags']
                  and pattern.fullmatch(vm['name']) and not vm['lock']]
    if not restorable:
        raise RollbackError('no retired version of %s to roll back to on %s (needs an unlocked template named '
                            '%s-retired-<UTC> with tag %s in VMID %s-%s)'
                            % (name, node, name, pitlib.TAG_RETIRED, conf['VMID_MIN'], conf['VMID_MAX']))
    target = max(restorable, key=lambda v: (retired_at(v['name']), v['vmid']))
    actives = [vm for vm in inventory if not vm['retired'] and vm['name'].lower() == name.lower()
               and in_range(vm['vmid'], conf)]
    current = current_of(actives) if actives else None

    kept_tags = [t for t in target['tags'] if t != pitlib.TAG_RETIRED]
    log('rolling back %s: restoring VM %d (%s)' % (name, target['vmid'], target['name']))
    # 先恢复、再退役：两步之间两个同名模板都在，平台克隆哪个都能用，没有空窗
    pitlib.run_cmd(['qm', 'set', target['vmid'], '--name', name]
                   + (['--tags', pitlib.format_tags(kept_tags)] if kept_tags else ['--delete', 'tags']), timeout=120, log=log)
    warnings, retired, failed = [], [], []
    for old in actives:
        (retired if retire(old, name, warnings) else failed).append(old['vmid'])
    if failed:
        # 退役失败就撤销恢复：两个同名在役时 current_of 挑构建时间新的——正是要回滚掉的坏构建
        undo = ['qm', 'set', target['vmid'], '--name', target['name'], '--tags', pitlib.format_tags(target['tags'])]
        try:
            pitlib.run_cmd(undo, timeout=120, log=log)
            undone = 'rollback undone'
        except pitlib.PitError as e:
            undone = 'undo failed too (%s); fix by hand: %s' % (e, ' '.join(map(str, undo)))
        raise RollbackError('could not retire VM %s (%s); %s' % (','.join(map(str, failed)), '; '.join(warnings), undone))

    bad_src = ((current or {}).get('meta') or {}).get('source') or {}
    holds[name] = {'at': int(now), 'by': 'rollback', 'bad_vmid': current['vmid'] if current else None,
                   'bad_source': bad_src.get('sha256') or bad_src.get('url'), 'restored_vmid': target['vmid'],
                   # 自动解除 hold 时拿来和上游比的：坏构建的下载地址与上游校验和（同一地址重新发布靠后者）
                   'bad_url': bad_src.get('url'), 'bad_checksum': bad_src.get('checksum')}
    volid = pitlib.find_os_disk_volid(target['config']) or ''
    restored_meta = target['meta'] or {}
    row = {'name': name, 'action': 'rolled-back', 'reason': 'rollback',
           'detail': 'restored VM %d (retired %s); retired %s; held until a new upstream image or --release'
                     % (target['vmid'], fmt_time(retired_at(target['name'])),
                        'VM ' + ','.join(map(str, retired)) if retired else 'nothing (no active template)'),
           'restored_vmid': target['vmid'], 'retired': retired, 'old_vmids': [v['vmid'] for v in actives],
           'storage': volid.split(':', 1)[0] if ':' in volid else None,
           'format': 'qcow2' if volid.endswith('.qcow2') else ('raw' if volid.endswith('.raw') else None),
           'bridge': pitlib.net_bridge(target['config']),
           'hookscript': bool(str(target['config'].get('hookscript') or '').strip()), 'next_vmid': None,
           'source': source_name((restored_meta.get('source') or {}).get('url'))}
    if warnings:
        row['warnings'] = warnings
    # 合并进上一轮的状态：其它模板的行保留（平台按行显示每个模板的状态），这个模板的行换成回滚结果
    doc = dict(previous)
    rows = [r for r in (previous.get('templates') or [])
            if isinstance(r, dict) and str(r.get('name', '')).lower() != name.lower()] + [row]
    for key in ('failures', 'pending_forced', 'sources_seen'):
        doc[key] = state_dict(previous, key)
    doc.pop('current', None)
    summary = summarize(rows)
    doc.update(tool='pve-import-template', version=imp.TOOL_VERSION, node=node, operation='rollback', dry_run=False,
               started_at=int(now), finished_at=int(time.time()), running=False, pid=os.getpid(),
               paused=conf['PAUSED'] == '1', templates=rows, summary=summary, holds=holds,
               ok=summary.get('failed', 0) == 0, vmid_high_water=int(previous.get('vmid_high_water', 0) or 0),
               message='rolled back %s to VM %d' % (name, target['vmid']))
    save_state(doc)
    log(doc['message'])
    return doc


def release(args, conf: dict) -> dict:
    """删除 hold（大小写不敏感匹配）。没有这个 hold 也算成功（幂等）。"""
    doc = load_state()
    holds = state_dict(doc, 'holds')
    key = next((k for k in holds if k.lower() == args.release.lower()), None)
    if key is not None:
        holds.pop(key)
        message = 'released the hold on %s' % key
    else:
        message = 'no hold on %s' % args.release
    doc['holds'] = holds
    doc['message'] = message
    save_state(doc)
    log(message)
    return doc


# ---------------------------------------------------------------- 主流程

def run_sync(args, conf: dict) -> dict:
    imp = load_import_module()
    if conf.get('CACHE_DIR'):
        imp.CACHE_DIR = conf['CACHE_DIR']
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
        'smoke_deep': conf['SMOKE_DEEP'] == '1',
        'min_mem': int(conf['MIN_FREE_MEM_MB']),
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
    source_delay = int(conf['SOURCE_DELAY_HOURS']) * 3600
    budget = int(conf['RUN_BUDGET_HOURS']) * 3600
    lo, hi = int(conf['VMID_MIN']), int(conf['VMID_MAX'])
    default_bridge = (getattr(imp, 'VM_DEFAULTS', None) or {}).get('bridge', 'vmbr0')

    node = pitlib.local_node_name()
    if not node:
        raise pitlib.PitError('cannot determine the local PVE node name')

    now = time.time()
    previous = load_state()
    failures = state_dict(previous, 'failures')
    holds = state_dict(previous, 'holds')
    pending = state_dict(previous, 'pending_forced')
    seen = state_dict(previous, 'sources_seen')
    rows = []
    global _REPORT
    # templates 直接引用 rows：中途存盘 / 被中断时存下的是最新的明细，不是开跑时的空列表
    report = _REPORT = {
        'tool': 'pve-import-template', 'version': imp.TOOL_VERSION, 'node': node, 'dry_run': args.dry_run,
        'started_at': int(now), 'finished_at': None, 'running': True, 'pid': os.getpid(),
        'selector': selector, 'templates': rows, 'failures': failures, 'holds': holds, 'pending_forced': pending,
        'sources_seen': seen, 'paused': conf['PAUSED'] == '1', 'message': None, 'summary': summarize([]),
        'vmid_high_water': int(previous.get('vmid_high_water', 0) or 0),
    }
    if args.dry_run and report['paused']:
        report['message'] = 'paused (PAUSED=1): a real run would do nothing; showing the plan anyway'
    cleanup_notes = []
    if not args.dry_run:
        save_state(dict(report))
        cleanup_notes = cleanup_stale_builds(node, conf, args.dry_run, cache_dir_of(conf))

    selected = select_templates(imp, config, selector)
    if selector and not selected:
        raise pitlib.PitError('selector %r matches no template in templates.yaml' % selector)
    addable = {t['name'] for t in select_templates(imp, config, missing_selector) if not pitlib.is_eol(t)} if add_missing else set()

    inventory = node_inventory(node)
    used = pitlib.cluster_used_vmids()
    cpu_level = pitlib.host_cpu_level()
    default_hook = node_hookscript(inventory, conf)
    storage_types, storage_problems, sums = {}, {}, {}

    def disk_format(storage: str, volid: str) -> Optional[str]:
        if volid and volid.endswith(('.qcow2', '.raw')):
            return pitlib.disk_format_for(storage, volid)
        if storage not in storage_types:
            storage_types[storage] = pitlib.storage_type(storage)
        return 'qcow2' if storage_types[storage] in pitlib.FILE_STORAGE_TYPES else None

    def checksum_of(tpl: dict, url: str):
        key = (tpl['name'], url)
        if key not in sums:
            sums[key] = imp.upstream_checksum(tpl, url, config)
        return sums[key]

    candidates = []
    for tpl in selected:
        name = tpl['name']
        # 只管平台认得的区间（9001-9999）：区间外的同名模板多半是运维手工做的，不重建、不退役、不回收
        actives = [vm for vm in inventory if not vm['retired'] and vm['name'].lower() == name.lower() and in_range(vm['vmid'], conf)]
        current = current_of(actives) if actives else None
        row = {'name': name, 'action': None, 'reason': None, 'detail': None, 'old_vmids': [v['vmid'] for v in actives],
               'storage': None, 'format': None, 'bridge': None, 'hookscript': None, 'next_vmid': None, 'source': None}
        rows.append(row)
        if current is not None:
            volid = pitlib.find_os_disk_volid(current['config']) or ''
            row['storage'] = volid.split(':', 1)[0] if ':' in volid else new_storage
            row['format'] = disk_format(row['storage'], volid) if row['storage'] else None
            row['bridge'] = pitlib.net_bridge(current['config']) or default_bridge
            row['hookscript'] = bool(str(current['config'].get('hookscript') or '').strip())
            row['source'] = source_name(((current['meta'] or {}).get('source') or {}).get('url'))
        else:
            row['storage'] = new_storage
            row['format'] = disk_format(new_storage, '') if new_storage else None
            row['bridge'] = conf['BRIDGE'] or default_bridge
            row['hookscript'] = bool(default_hook)
        recipe = imp.recipe_of(tpl, opts['tuning'], opts['upgrade'], mirror_name)
        hold = holds.get(name) if isinstance(holds.get(name), dict) else None
        is_pending = not args.force and isinstance(pending.get(name), dict)
        forced = args.force or is_pending

        level = tpl.get('min_cpu_level')
        if level and cpu_level is not None and cpu_level < int(level) and (actives or name in addable):
            # 宿主机 CPU 跑不了这个系统（EL10 要 x86-64-v3）：不建、不补，也不算失败
            row.update(action='skipped', detail='host CPU lacks x86-64-v%d' % int(level))
            pending.pop(name, None)
            continue

        if not actives:
            if name not in addable:
                why = 'end-of-life' if add_missing and pitlib.is_eol(tpl) else 'not on this node (use --add-missing to import it)'
                row.update(action='absent', detail=why)
                pending.pop(name, None)
                continue
            if not new_storage:
                row.update(action='failed', detail='no storage configured for new templates (--storage / STORAGE)')
                continue
            if new_storage not in storage_problems:
                storage_problems[new_storage] = pitlib.storage_template_problem(new_storage)
            if storage_problems[new_storage]:
                # 厚置备 LVM / iSCSI 之类放不了模板：别下载几 GB 再在 qm template 那一步失败
                row.update(action='skipped', reason='missing', detail=storage_problems[new_storage])
                continue
            url, how = pitlib.resolve_source(tpl, log=log, config=config)
            row['source'] = source_name(url)
            detail = 'not on this node'
            if hold:
                if not hold_released(hold, tpl, url, how, checksum_of):
                    row.update(action='held', reason='missing', detail=held_detail(hold, args.force))
                    continue
                holds.pop(name, None)
                detail = 'hold released: new upstream image; ' + detail
            if not args.force and (wait := backoff_remaining(failures.get(name), tpl, recipe, backoff, now)) > 0:
                row.update(action='backoff', reason='missing', detail='import failed before; retry in %d h: %s'
                           % (wait // 3600 + 1, (failures[name] or {}).get('error', '?')))
                continue
            candidates.append((row, tpl, actives, 'missing', detail, url))
            continue

        try:
            d = decide(tpl, current, imp=imp, force=forced, max_age_days=0 if pitlib.is_eol(tpl) else max_age,
                       now=now, recipe=recipe, config=config, checksum_of=checksum_of)
        except Exception as e:  # noqa: BLE001 - 一个模板探测出错不影响其它模板
            row.update(action='failed', detail='probe failed: %s' % e)
            continue
        reason, detail, url = d['reason'], d['detail'], d['url']
        row['source'] = source_name(url)
        if d['warnings']:
            row['warnings'] = list(d['warnings'])
        if hold:
            try:
                released = hold_released(hold, tpl, url, d['how'], checksum_of)
            except Exception as e:  # noqa: BLE001 - 探测出错就继续 hold
                log('Warning: cannot check the hold on %s: %s' % (name, e))
                released = False
            if not released:
                row.update(action='held', reason=reason, detail=held_detail(hold, args.force))
                continue
            holds.pop(name, None)
            log('%s: hold released (new upstream image %s)' % (name, source_name(url)))
            detail = 'hold released: new upstream image; ' + detail
        if is_pending and reason == 'forced':
            detail = 'pending forced rebuild since %s' % fmt_time(pending[name].get('at'))
        if reason == 'legacy' and not adopt_legacy:
            row.update(action='skipped', reason='legacy', detail='imported by an older tool; ADOPT_LEGACY=0 keeps it as is')
            continue
        if reason is None:
            seen.pop(name, None)
            row.update(action='current', detail=detail)
            # 上一轮改名退役撞上克隆锁留下的同名多个在役：只留当前那个，其余补退役
            if len(actives) > 1:
                others = [old for old in actives if old['vmid'] != current['vmid']]
                if args.dry_run:
                    row['would_retire'] = [old['vmid'] for old in others]
                else:
                    extra = [old['vmid'] for old in others if retire(old, name, row.setdefault('warnings', []))]
                    if extra:
                        row['retired'] = extra
            continue
        if reason == 'source-changed' and source_delay > 0:
            # 金丝雀：新镜像第一次见到后满 SOURCE_DELAY_HOURS 才采用（SOURCE_DELAY_HOURS=0 的节点先吃）
            key = d['source_id'] or row['source']
            entry = seen.get(name)
            if not isinstance(entry, dict) or entry.get('key') != key or not entry.get('first_seen'):
                entry = seen[name] = {'key': key, 'first_seen': int(now)}
            age = int(now) - int(float(entry['first_seen']))
            if age < source_delay:
                row.update(action='waiting', reason=reason,
                           detail='%s; first seen %s ago, adopting in %s (SOURCE_DELAY_HOURS=%d)'
                                  % (detail, fmt_age(age), fmt_age(source_delay - age), source_delay // 3600))
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
    appliance_down = None
    sim_used = set(used)
    sim_hw = pitlib.combined_high_water(report['vmid_high_water'], lo, hi)
    for index, (row, tpl, actives, reason, detail, url) in enumerate(candidates):
        name = tpl['name']
        row.update(reason=reason, detail=detail)
        # 不能建的在规划时就判掉，不占本轮名额（旧版先占名额再在构建里抛 Skip：排在前面的 OVMF 模板每轮都挤掉别的）
        current = current_of(actives) if actives else None
        if current is not None and str(current['config'].get('bios', 'seabios')) != 'seabios':
            row.update(action='skipped', detail='%s (VM %d) uses %s firmware; only SeaBIOS templates are rebuilt automatically'
                       % (name, current['vmid'], current['config'].get('bios')))
            pending.pop(name, None)
            continue
        storage = new_storage
        volid = ''
        if current is not None:
            volid = pitlib.find_os_disk_volid(current['config']) or ''
            storage = volid.split(':', 1)[0] if ':' in volid else new_storage
        if not storage:
            row.update(action='failed', detail='cannot determine the storage of the current template')
            continue

        deferred = None
        if appliance_down:
            deferred = 'libguestfs appliance cannot start on this host (%s); not attempted this run' % appliance_down
        elif max_builds and builds >= max_builds:
            deferred = detail     # 2.1.0 起的语义：名额满了只改 action，detail 仍是重建原因
        elif budget and not args.dry_run and time.time() - now >= budget:
            deferred = 'run budget exhausted'
        if deferred is not None:
            row.update(action='deferred', detail=deferred)
            if reason == 'forced' and not args.dry_run:
                mark_pending(pending, name, now)
            continue
        if args.dry_run:
            builds += 1
            row['action'] = 'would-build'
            with contextlib.suppress(pitlib.PitError):
                row['next_vmid'] = sim_hw = pitlib.allocate_vmid(sim_used, lo, hi, sim_hw)
                sim_used.add(sim_hw)
            continue
        try:
            pitlib.ensure_memory(opts['min_mem'])
        except pitlib.Deferred as e:
            row.update(action='deferred', detail=str(e))
            if reason == 'forced':
                mark_pending(pending, name, now)
            continue
        builds += 1
        started = time.time()
        report['current'] = name
        save_state(dict(report))
        recipe = imp.recipe_of(tpl, opts['tuning'], opts['upgrade'], mirror_name)
        try:
            if url is None:
                url = pitlib.resolve_source_url(tpl, log=log, config=config)
            result = build_one(imp, tpl, node, actives, storage=storage, used=used, conf=conf, opts=opts,
                               mirror_config=mirror_config, mirror_name=mirror_name, url=url, state=report,
                               config=config, inventory=inventory, current_volid=volid)
            row.update(action='built', new_vmid=result['new_vmid'], retired=result['retired'],
                       built_at=result['meta']['built_at'], source=source_name(result['meta']['source'].get('url')),
                       source_url=result['meta']['source'].get('url'),
                       checksum_verified=result['meta']['checksum_verified'],
                       warnings=row.get('warnings', []) + result['warnings'])
            failures.pop(name, None)
            pending.pop(name, None)
            seen.pop(name, None)
            save_state(dict(report))  # 高水位立即落盘：后面的模板失败 / 被杀也不会回头复用号
        except Skip as e:
            row.update(action='skipped', detail=str(e)[:500])
            pending.pop(name, None)
        except pitlib.Deferred as e:
            # 空间不够 / 冒烟前内存不够：不算失败、不进退避、不占名额
            row.update(action='deferred', detail=str(e)[:500])
            builds -= 1
            if reason == 'forced':
                mark_pending(pending, name, now)
        except KeyboardInterrupt:
            row.update(action='failed', detail='interrupted', failure='interrupted')
            prev = failures.get(name) or {}
            failures[name] = {'at': int(time.time()), 'recipe': recipe, 'url': source_key(tpl), 'kind': 'interrupted',
                              'count': int(prev.get('count', 0)) if prev.get('recipe') == recipe else 0,
                              'error': 'interrupted'}
            # 被打断的强制重建（这一个和还没轮到的）都记成 pending，下一轮接着建
            for later in candidates[index:]:
                if later[3] == 'forced':
                    mark_pending(pending, later[1]['name'], now)
            row['duration_s'] = int(time.time() - started)
            raise
        except Exception as e:  # noqa: BLE001 - 单模板失败隔离
            msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
            kind = failure_kind(e)
            row.update(action='failed', detail=msg[:500], failure=kind)
            prev = failures.get(name) or {}
            failures[name] = {'at': int(time.time()), 'recipe': recipe, 'url': source_key(tpl), 'kind': kind,
                              'count': int(prev.get('count', 0)) + 1 if prev.get('recipe') == recipe else 1,
                              'error': msg[:300]}
            log('FAILED %s (%s): %s' % (name, kind, msg))
            if isinstance(e, pitlib.LaunchError):
                # appliance 起不来是宿主机的问题：本轮剩下的不再逐个下载、逐个失败
                appliance_down = 'see the failure of %s; run setup.sh --full-check' % name
                report['message'] = 'libguestfs appliance cannot start on this host; remaining builds deferred'
                log('libguestfs appliance cannot start on this host (even with TCG); run setup.sh --full-check')
        row['duration_s'] = int(time.time() - started)

    # 回收：本节点上有 pit-retired 的、templates.yaml 里的每个名字（不只本轮处理过的）
    vms_now = pitlib.node_vms(node)
    by_name = {r['name']: r for r in rows}
    others = {}
    for tpl in config.get('templates', []) or []:
        name = tpl['name']
        if not any(vm['template'] and pitlib.TAG_RETIRED in vm['tags'] and pitlib.retired_of(vm['name'], name)
                   and in_range(vm['vmid'], conf) for vm in vms_now):
            continue
        deleted, notes = collect_garbage(node, name, keep, time.time(), args.dry_run, vmid_range=(lo, hi), vms=vms_now)
        target = by_name[name] if name in by_name else others.setdefault(name, {})
        if deleted:
            target['would_delete' if args.dry_run else 'deleted'] = deleted
        if notes:
            target['gc_notes'] = notes
    if others:
        report['gc'] = others
    for owner, note in cleanup_notes:
        target = by_name.get(owner) if owner else None
        (target.setdefault('gc_notes', []) if target is not None else report.setdefault('gc_notes', [])).append(note)
    if args.dry_run:
        for row in rows:
            if row['action'] == 'current' and row.get('would_retire'):
                row['action'] = 'would-retire'
            elif row['action'] == 'current' and row.get('would_delete'):
                row['action'] = 'would-delete'
    if not report.get('message') and any(r['action'] == 'deferred' and r.get('detail') == 'run budget exhausted' for r in rows):
        report['message'] = 'run budget exhausted (RUN_BUDGET_HOURS=%s); remaining builds deferred' % conf['RUN_BUDGET_HOURS']

    summary = summarize(rows)
    report.update(summary=summary, running=False, finished_at=int(time.time()), ok=summary.get('failed', 0) == 0)
    report.pop('current', None)
    return report


def print_human(report: dict):
    counts = ', '.join('%s %d' % kv for kv in sorted((report.get('summary') or {}).items()) if kv[1])
    print('\n===== sync-templates on %s: %s =====' % (report.get('node'), counts or 'nothing to do'))
    if report.get('message'):
        print('  ' + report['message'])
    for name, hold in sorted((report.get('holds') or {}).items()):
        if isinstance(hold, dict):
            print('  hold: %s since %s (%s)' % (name, fmt_time(hold.get('at')), hold.get('by') or 'manual'))
    for row in report.get('templates') or []:
        extra = ''
        if row.get('new_vmid'):
            extra = ' → VM %d (retired %s)' % (row['new_vmid'], ','.join(map(str, row.get('retired', []))) or '-')
        if row.get('restored_vmid'):
            extra = ' → VM %d restored' % row['restored_vmid']
        if row.get('would_retire'):
            extra += ' would retire %s' % ','.join(map(str, row['would_retire']))
        if row.get('deleted'):
            extra += ' deleted %s' % ','.join(map(str, row['deleted']))
        if row.get('would_delete'):
            extra += ' would delete %s' % ','.join(map(str, row['would_delete']))
        print('  %-12s %-24s %s%s' % (row.get('action'), row.get('name'), row.get('detail') or '', extra))
        if row.get('action') == 'would-build':
            print('  %-12s %-24s storage=%s fmt=%s bridge=%s hook=%s vmid≈%s src=%s reason=%s%s' % (
                '', '', row.get('storage') or '-', row.get('format') or 'default', row.get('bridge') or '-',
                'yes' if row.get('hookscript') else 'no', row.get('next_vmid') or '?', row.get('source') or '-',
                row.get('reason') or '-',
                ' would retire %s' % ','.join(map(str, row['old_vmids'])) if row.get('old_vmids') else ''))
        for w in row.get('warnings') or []:
            print('  %-12s %-24s warning: %s' % ('', '', w))
    for name, gc in sorted((report.get('gc') or {}).items()):
        done = gc.get('deleted') or gc.get('would_delete')
        if done:
            print('  %-12s %-24s %s %s' % ('gc', name, 'would delete' if gc.get('would_delete') else 'deleted',
                                          ','.join(map(str, done))))


def emit(report: dict, as_json: bool):
    if as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        print_human(report)


def main(argv) -> int:
    args = parse_args(argv)
    if args.help:
        print(__doc__)
        return 0
    real_run = not (args.list or args.dry_run or args.rollback or args.release)
    tools = () if args.release else ('pvesh', 'qm') + (('virt-customize', 'qemu-img') if real_run else ())
    for tool in tools:
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
        list_mode(node, conf, args.json)
        return 0

    if args.release:
        with pitlib.ToolLock():
            doc = release(args, conf)
        emit(doc, args.json)
        return 0

    if args.rollback:
        with pitlib.ToolLock():
            if pitlib.cluster_quorate() is False:
                print('Error: cluster has no quorum; cannot roll back now', file=sys.stderr)
                return 4
            try:
                doc = rollback(args, conf)
            except RollbackError as e:
                print('Error: %s' % e, file=sys.stderr)
                return 1
        emit(doc, args.json)
        return 0

    if args.dry_run:
        report = run_sync(args, conf)
    else:
        with pitlib.ToolLock():
            if conf['PAUSED'] == '1':
                # 拿到锁之后、做任何事之前：不开日志、不查集群、不动任何模板
                print('PAUSED=1 in the config: nothing to do', file=sys.stderr)
                emit(paused_report(), args.json)
                return 0
            log_path = open_log()
            log('sync-templates started (log %s)' % log_path)
            if pitlib.cluster_quorate() is False:
                # 没 quorum 时 /etc/pve 只读：跑下去每个模板都会失败、进退避。整轮跳过，不记 failures
                msg = 'cluster has no quorum; skipped this run'
                log(msg)
                record_failure(msg)
                print('Error: %s' % msg, file=sys.stderr)
                return 4
            try:
                report = run_sync(args, conf)
            except BaseException as e:
                doc = dict(_REPORT) if _REPORT is not None else load_state()
                if _REPORT is not None:
                    doc['summary'] = summarize(doc.get('templates') or [])
                doc.update(running=False, finished_at=int(time.time()), ok=False,
                           message=('interrupted' if isinstance(e, KeyboardInterrupt) else str(e))[:500])
                doc.pop('current', None)
                save_state(doc)
                raise
            report['log'] = log_path
            save_state(report)

    emit(report, args.json)
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
