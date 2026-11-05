#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tui.py - pve-import-template 交互式导入向导

在终端里一步步选择存储、起始 VMID、要导入的系统模板与各项选项，最后调用 import.py 执行导入。

- 有 whiptail / dialog 时使用对话框界面；否则回退为纯文本菜单（--text 可强制文本模式）。
- 默认只勾选「主流系列」（templates.yaml 的 default_families：ubuntu / debian / centos / alpine），
  其他系列（Rocky / Alma / Fedora / openSUSE / Arch ...）作为可选。
- 上一次的选择会保存到 ~/.config/pve-import-template/last.json，下次作为默认值。
- 通过管道执行（curl ... | bash）时会自动接管 /dev/tty。

用法
  python3 tui.py                       # 入口菜单：导入新模板 / 更新已导入的模板
  python3 tui.py import [storage] [start-vmid] [template-filter] [import.py 的选项...]
  python3 tui.py update [选择器...] [update-templates.py 的选项...]
  通用选项：--text（纯文本界面）、--dry-run、--no-save（不记住选择）
  （所有参数只作为向导的默认值，最终以向导里的选择为准）
"""

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from typing import List, Optional, Sequence, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IMPORT_PY = os.path.join(SCRIPT_DIR, 'import.py')
UPDATE_PY = os.path.join(SCRIPT_DIR, 'update-templates.py')
STATE_FILE = os.path.expanduser('~/.config/pve-import-template/last.json')

MIRROR_LABELS = {
    'aliyun': '阿里云',
    'tsinghua': '清华大学 TUNA',
    'huawei': '华为云',
    'tencent': '腾讯云',
    'ustc': '中国科学技术大学',
}

PRESET_MAINSTREAM = 'main'
PRESET_ALL = 'all'
PRESET_FAMILY = 'family'
PRESET_CUSTOM = 'custom'


def load_module(name: str, path: str):
    """按路径加载脚本为模块（import.py / update-templates.py 的文件名都不能直接 import）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_import_module():
    return load_module('pveimport', IMPORT_PY)


def load_update_module():
    return load_module('pveupdate', UPDATE_PY)


class Cancel(Exception):
    """用户在当前步骤选择了「返回 / 取消」。"""


# ---------------------------------------------------------------------------
# UI 后端
# ---------------------------------------------------------------------------
class DialogUI:
    """whiptail / dialog 对话框后端。"""

    def __init__(self, binary: str):
        self.binary = binary
        self.is_dialog = os.path.basename(binary) == 'dialog'
        self.env = os.environ.copy()
        lang = self.env.get('LC_ALL') or self.env.get('LC_CTYPE') or self.env.get('LANG') or ''
        if 'utf-8' not in lang.lower() and 'utf8' not in lang.lower():
            self.env['LC_ALL'] = 'C.UTF-8'

    def _geometry(self, items: int = 0, extra_lines: int = 0) -> Tuple[int, int, int]:
        cols, rows = shutil.get_terminal_size((80, 24))
        width = max(60, min(cols - 4, 96))
        max_height = max(12, rows - 3)
        list_height = max(1, min(items, max_height - 9 - extra_lines))
        height = min(max_height, list_height + 9 + extra_lines) if items else min(max_height, 8 + extra_lines)
        return height, width, list_height

    def _buttons(self, ok: Optional[str] = None, cancel: Optional[str] = None,
                 yes: Optional[str] = None, no: Optional[str] = None) -> List[str]:
        """whiptail 与 dialog 的按钮文字选项名不同（--ok-button vs --ok-label）。"""
        suffix = 'label' if self.is_dialog else 'button'
        args: List[str] = []
        for name, value in (('ok', ok), ('cancel', cancel), ('yes', yes), ('no', no)):
            if value is not None:
                args += [f'--{name}-{suffix}', value]
        return args

    def _run(self, title: str, args: Sequence[str]) -> str:
        read_fd, write_fd = os.pipe()
        cmd = [self.binary, '--title', title, '--backtitle', 'pve-import-template 导入向导',
               '--output-fd', str(write_fd)]
        if self.is_dialog:
            cmd.append('--no-collapse')
        cmd.extend(args)
        try:
            proc = subprocess.run(cmd, pass_fds=(write_fd,), env=self.env)
        finally:
            os.close(write_fd)
        with os.fdopen(read_fd) as reader:
            output = reader.read()
        if proc.returncode != 0:
            raise Cancel()
        return output

    def menu(self, title: str, text: str, items: List[Tuple[str, str]], default: Optional[str] = None,
             ok_label: str = '确定', cancel_label: str = '返回') -> str:
        lines = text.count('\n') + 1
        height, width, list_height = self._geometry(len(items), lines)
        args = self._buttons(ok=ok_label, cancel=cancel_label)
        if default is not None:
            args += ['--default-item', default]
        args += ['--menu', text, str(height), str(width), str(list_height)]
        for tag, desc in items:
            args += [tag, desc]
        return self._run(title, args).strip()

    def checklist(self, title: str, text: str, items: List[Tuple[str, str, bool]],
                  ok_label: str = '确定', cancel_label: str = '返回') -> List[str]:
        lines = text.count('\n') + 1
        height, width, list_height = self._geometry(len(items), lines)
        args = self._buttons(ok=ok_label, cancel=cancel_label) + ['--separate-output',
                '--checklist', text, str(height), str(width), str(list_height)]
        for tag, desc, checked in items:
            args += [tag, desc, 'ON' if checked else 'OFF']
        return [line.strip() for line in self._run(title, args).splitlines() if line.strip()]

    def inputbox(self, title: str, text: str, default: str = '') -> str:
        lines = text.count('\n') + 1
        height, width, _ = self._geometry(0, lines + 3)
        args = self._buttons(ok='确定', cancel='返回') + ['--inputbox', text, str(height), str(width), default]
        return self._run(title, args).strip()

    def yesno(self, title: str, text: str, yes_label: str = '是', no_label: str = '否') -> bool:
        lines = text.count('\n') + 1
        height, width, _ = self._geometry(0, lines + 2)
        args = self._buttons(yes=yes_label, no=no_label) + ['--yesno', text, str(height), str(width)]
        try:
            self._run(title, args)
            return True
        except Cancel:
            return False

    def msgbox(self, title: str, text: str):
        lines = max(text.count('\n') + 1, 1)
        height, width, _ = self._geometry(0, lines + 2)
        args = self._buttons(ok='确定') + ['--msgbox', text, str(height), str(width)]
        try:
            self._run(title, args)
        except Cancel:
            pass


class TextUI:
    """纯文本后端（无 whiptail/dialog 时使用）。"""

    @staticmethod
    def _header(title: str, text: str):
        print(f'\n=== {title} ===')
        if text:
            print(text)

    @staticmethod
    def _ask(prompt: str) -> str:
        try:
            return input(prompt).strip()
        except EOFError:
            print()
            raise Cancel()

    def menu(self, title, text, items, default=None, ok_label='确定', cancel_label='返回'):
        self._header(title, text)
        if default not in [tag for tag, _ in items]:
            default = None
        width = max(len(tag) for tag, _ in items)
        for idx, (tag, desc) in enumerate(items, 1):
            mark = '*' if tag == default else ' '
            print(f'  {mark}{idx:>2}) {tag:<{width}}  {desc}')
        while True:
            answer = self._ask(f'请选择编号 [1-{len(items)}]（b={cancel_label}）: ')
            if answer.lower() == 'b':
                raise Cancel()
            if answer == '' and default is not None:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(items):
                return items[int(answer) - 1][0]
            print('无效输入。')

    def checklist(self, title, text, items, ok_label='确定', cancel_label='返回'):
        self._header(title, text)
        state = [checked for _, _, checked in items]
        width = max(len(tag) for tag, _, _ in items)
        while True:
            for idx, (tag, desc, _) in enumerate(items, 1):
                mark = 'x' if state[idx - 1] else ' '
                print(f'  [{mark}] {idx:>2}) {tag:<{width}}  {desc}')
            answer = self._ask('输入编号切换选中（可空格分隔多个；a=全选 n=清空 回车=确定 b=返回）: ')
            if answer.lower() == 'b':
                raise Cancel()
            if answer == '':
                return [tag for (tag, _, _), on in zip(items, state) if on]
            if answer.lower() == 'a':
                state = [True] * len(items)
            elif answer.lower() == 'n':
                state = [False] * len(items)
            else:
                for token in answer.replace(',', ' ').split():
                    if token.isdigit() and 1 <= int(token) <= len(items):
                        state[int(token) - 1] = not state[int(token) - 1]
                    else:
                        print(f'忽略无效编号：{token}')

    def inputbox(self, title, text, default=''):
        self._header(title, text)
        answer = self._ask(f'输入（回车使用默认 [{default}]，b=返回）: ')
        if answer.lower() == 'b':
            raise Cancel()
        return answer or default

    def yesno(self, title, text, yes_label='是', no_label='否'):
        self._header(title, text)
        while True:
            try:
                answer = self._ask(f'{yes_label}/{no_label} [y/n]: ').lower()
            except Cancel:
                return False
            if answer in ('y', 'yes', '是'):
                return True
            if answer in ('n', 'no', '否'):
                return False

    def msgbox(self, title, text):
        self._header(title, text)
        try:
            self._ask('按回车继续...')
        except Cancel:
            pass


def pick_ui(force_text: bool):
    if not force_text:
        for binary in ('whiptail', 'dialog'):
            path = shutil.which(binary)
            if path:
                return DialogUI(path)
    return TextUI()


def ensure_tty(force_text: bool = False):
    """通过管道运行（curl | bash）时 stdin/stdout 不是终端，尝试接管 /dev/tty。

    纯文本模式不依赖终端控制，允许从管道读取输入（便于脚本化 / 测试）。
    """
    if force_text or (os.isatty(0) and os.isatty(1)):
        return
    try:
        fd = os.open('/dev/tty', os.O_RDWR)
    except OSError:
        print('错误：交互向导需要终端。非交互环境请直接使用 python3 import.py <storage> <vmid> ...', file=sys.stderr)
        sys.exit(2)
    if not os.isatty(0):
        os.dup2(fd, 0)
    if not os.isatty(1):
        os.dup2(fd, 1)
    os.close(fd)


# ---------------------------------------------------------------------------
# PVE 环境信息
# ---------------------------------------------------------------------------
def local_node_name() -> Optional[str]:
    try:
        return os.path.basename(os.readlink('/etc/pve/local').rstrip('/')) or None
    except OSError:
        pass
    try:
        out = subprocess.check_output(['hostname'], stderr=subprocess.DEVNULL)
        return out.decode(errors='ignore').strip().split('.')[0] or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def pvesh_json(path: str, *extra: str):
    try:
        out = subprocess.check_output(['pvesh', 'get', path, *extra, '--output-format', 'json'],
                                      stderr=subprocess.DEVNULL)
        return json.loads(out)
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return None


def list_image_storages() -> Optional[List[dict]]:
    """返回支持 images 的存储列表（含类型 / 剩余空间），pvesh 不可用时返回 None。"""
    node = local_node_name()
    data = pvesh_json(f'/nodes/{node}/storage', '--content', 'images') if node else None
    if data is None:
        data = pvesh_json('/storage')
        if data is None:
            return None
    result = []
    for st in data:
        content = str(st.get('content', '')).split(',')
        if 'images' not in content:
            continue
        if st.get('enabled', 1) in (0, '0', False):
            continue
        result.append({
            'name': st.get('storage'),
            'type': st.get('type', '?'),
            'avail': st.get('avail'),
            'active': st.get('active', 1),
        })
    return sorted(result, key=lambda s: s['name'])


def list_bridges() -> List[str]:
    bridges = []
    try:
        for name in sorted(os.listdir('/sys/class/net')):
            if os.path.isdir(f'/sys/class/net/{name}/bridge'):
                bridges.append(name)
    except OSError:
        pass
    return bridges


def human_size(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return '?'
    for unit in ('B', 'K', 'M', 'G', 'T'):
        if size < 1024 or unit == 'T':
            return f'{size:.0f}{unit}' if unit in ('B', 'K', 'M') else f'{size:.1f}{unit}'
        size /= 1024
    return '?'


def pve_version() -> str:
    try:
        out = subprocess.check_output(['pveversion'], stderr=subprocess.DEVNULL).decode(errors='ignore')
        return out.strip().split()[0]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return '未检测到 PVE'


# ---------------------------------------------------------------------------
# 状态持久化
# ---------------------------------------------------------------------------
def load_state() -> dict:
    try:
        with open(STATE_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def mirror_menu(ui, mirrors: dict, current: Optional[str], title: str, text: str) -> Optional[str]:
    """镜像源选择菜单（导入 / 更新向导共用）；返回镜像名或 None。"""
    items = [('none', '不换源（使用各发行版官方源）')]
    for name, cfg in mirrors.items():
        fams = ', '.join(k for k in cfg.keys() if isinstance(cfg.get(k), dict)) if isinstance(cfg, dict) else ''
        label = MIRROR_LABELS.get(name, name)
        items.append((name, f'{label:<14} {name:<10} 支持: {fams}'))
    default = current or 'none'
    if default not in [i[0] for i in items]:
        default = 'none'
    choice = ui.menu(title, text, items, default=default)
    return None if choice == 'none' else choice


# ---------------------------------------------------------------------------
# 向导
# ---------------------------------------------------------------------------
class Wizard:
    def __init__(self, ui, pveimport, config: dict, state: dict, dry_run: bool = False):
        self.ui = ui
        self.pveimport = pveimport
        self.config = config
        self.state = state
        self.dry_run = dry_run
        self.printed = False
        self.templates = config.get('templates', [])
        self.mirrors = config.get('mirrors', {}) or {}
        self.defaults = pveimport.default_families(config)
        self.used_vmids, self.existing = pveimport.cluster_vm_index()
        self.storages = list_image_storages()
        self.node = local_node_name() or '?'

    # ---- 工具 ----
    def family_of(self, tpl: dict) -> str:
        return self.pveimport.template_family(tpl)

    def families(self) -> List[str]:
        seen = []
        for tpl in self.templates:
            fam = self.family_of(tpl)
            if fam not in seen:
                seen.append(fam)
        return seen

    def names_for_families(self, fams: Sequence[str]) -> List[str]:
        fams = {f.lower() for f in fams}
        return [t['name'] for t in self.templates if self.family_of(t) in fams]

    def selection_expr(self) -> Optional[str]:
        """把选中的模板压缩成 import.py 的选择器：整系列用系列名，其余用模板名；全选返回 None。

        开启 --only-new 时已存在的模板反正会被跳过，因此把它们视作「已选」以便压缩成系列名。
        """
        chosen = set(self.state['templates'])
        if not chosen:
            return ''
        if self.state.get('only_new'):
            chosen |= self.existing
        selected = [t['name'] for t in self.templates if t['name'] in chosen]
        if len(selected) == len(self.templates):
            return None
        parts = []
        chosen = set(selected)
        for fam in self.families():
            fam_names = self.names_for_families([fam])
            if all(n in chosen for n in fam_names):
                parts.append(fam)
            else:
                parts.extend(n for n in fam_names if n in chosen)
        return ','.join(parts)

    def build_args(self) -> List[str]:
        st = self.state
        args = [st['storage'], str(st['start_vmid'])]
        expr = self.selection_expr()
        if expr is None:
            args.append('--all')
        elif expr:
            args.append(expr)
        if st.get('only_new'):
            args.append('--only-new')
        if not st.get('tuning', True):
            args.append('--no-tuning')
        if st.get('refresh'):
            args.append('--refresh')
        if st.get('insecure'):
            args.append('--insecure')
        if st.get('mirror'):
            args += ['--mirror', st['mirror']]
        vm_defaults = self.pveimport.VM_DEFAULTS
        if st.get('bridge') and st['bridge'] != vm_defaults['bridge']:
            args += ['--bridge', st['bridge']]
        if st.get('memory') and int(st['memory']) != vm_defaults['memory']:
            args += ['--memory', str(st['memory'])]
        if st.get('cores') and int(st['cores']) != vm_defaults['cores']:
            args += ['--cores', str(st['cores'])]
        if self.dry_run:
            args.append('--dry-run')
        return args

    def command_line(self) -> str:
        return 'python3 import.py ' + ' '.join(shlex.quote(a) for a in self.build_args())

    # ---- 步骤 ----
    def step_storage(self):
        st = self.state
        cache_dir = getattr(self.pveimport, 'CACHE_DIR', os.path.join(SCRIPT_DIR, 'cloud_img'))
        try:
            probe = cache_dir if os.path.isdir(cache_dir) else os.path.dirname(cache_dir)
            cache_free = human_size(shutil.disk_usage(probe).free)
        except OSError:
            cache_free = '?'
        text = (f'节点：{self.node}    {pve_version()}\n'
                f'已存在 VM/模板：{len(self.existing)} 个    可用模板：{len(self.templates)} 个\n'
                f'镜像缓存：{cache_dir}（剩余 {cache_free}，可用 CLOUD_IMG_DIR 更改）\n\n'
                '请选择用于存放模板磁盘的存储：')
        if self.storages:
            items = []
            for s in self.storages:
                flags = [] if s.get('active', 1) in (1, '1', True) else ['未激活']
                avail = human_size(s['avail']) if s.get('avail') is not None else '?'
                desc = f'{s["type"]:<8} 可用 {avail:>7}' + (f'  [{", ".join(flags)}]' if flags else '')
                items.append((s['name'], desc))
            names = [i[0] for i in items]
            default = st.get('storage') if st.get('storage') in names else ('local-lvm' if 'local-lvm' in names else names[0])
            st['storage'] = self.ui.menu('1/6 选择存储', text, items, default=default)
        else:
            hint = '（未能通过 pvesh 读取存储列表，请手动输入存储名称，如 local-lvm）'
            value = ''
            while not value:
                value = self.ui.inputbox('1/6 选择存储', text + '\n' + hint, st.get('storage') or 'local-lvm')
            st['storage'] = value

    def step_vmid(self):
        st = self.state
        default = str(st.get('start_vmid') or 9001)
        while True:
            text = ('模板从该 VMID 起依次编号；集群内已被占用的 VMID 会自动跳过。\n'
                    '建议使用 9001-9999（rhapsi 平台只识别这个区间的模板）。')
            value = self.ui.inputbox('2/6 起始 VMID', text, default)
            if value.isdigit() and 100 <= int(value) <= 999999999:
                vmid = int(value)
                if vmid in self.used_vmids:
                    nxt = vmid
                    while nxt in self.used_vmids:
                        nxt += 1
                    if not self.ui.yesno('VMID 已被占用',
                                         f'VMID {vmid} 已被占用，导入时会自动从 {nxt} 开始分配。\n继续使用 {vmid} 吗？',
                                         '继续', '重新输入'):
                        default = str(nxt)
                        continue
                st['start_vmid'] = vmid
                return
            self.ui.msgbox('输入无效', 'VMID 必须是 100 ~ 999999999 之间的整数。')
            default = value or default

    def step_templates(self):
        st = self.state
        while True:
            current = set(st.get('templates') or [])
            preset_items = [
                (PRESET_MAINSTREAM, f'主流系列（推荐）：{" / ".join(self.pveimport.family_label(f) for f in self.defaults)}'),
                (PRESET_ALL, f'全部模板（{len(self.templates)} 个）'),
                (PRESET_FAMILY, '按系列选择（Rocky / Alma / Fedora / openSUSE / Arch ...）'),
                (PRESET_CUSTOM, '逐个勾选' + (f'（保留当前 {len(current)} 个选择）' if current else '')),
            ]
            default_preset = st.get('preset') or PRESET_MAINSTREAM
            preset = self.ui.menu('3/6 选择系统模板',
                                  '先选一个预设，下一步仍可逐个勾选微调：', preset_items, default=default_preset)
            st['preset'] = preset
            try:
                if self._pick_templates(preset, current):
                    return
            except Cancel:
                continue

    def _pick_templates(self, preset: str, current: set) -> bool:
        """按预设生成初始勾选并让用户微调；返回 True 表示已确定选择，Cancel 表示回到预设菜单。"""
        st = self.state
        if preset == PRESET_MAINSTREAM:
            preselected = set(self.names_for_families(self.defaults))
        elif preset == PRESET_ALL:
            preselected = {t['name'] for t in self.templates}
        elif preset == PRESET_FAMILY:
            prev = set(st.get('families') or self.defaults)
            items = []
            for fam in self.families():
                count = len(self.names_for_families([fam]))
                tag_note = '  [默认]' if fam in self.defaults else ''
                items.append((fam, f'{self.pveimport.family_label(fam):<14} {count} 个模板{tag_note}', fam in prev))
            fams = self.ui.checklist('3/6 选择系列', '空格勾选 / 取消，回车确认：', items)
            st['families'] = fams
            preselected = set(self.names_for_families(fams))
        else:
            preselected = current

        skip_existing = preset != PRESET_CUSTOM
        while True:
            items = []
            for tpl in self.templates:
                name = tpl['name']
                fam = self.pveimport.family_label(self.family_of(tpl))
                notes = []
                if name in self.existing:
                    notes.append('已存在')
                if tpl.get('net_tuning', True) is False:
                    notes.append('无调优')
                desc = f'{fam:<14}' + (f'  [{", ".join(notes)}]' if notes else '')
                checked = name in preselected and not (skip_existing and name in self.existing)
                items.append((name, desc, checked))
            text = '空格勾选 / 取消，回车确认。已存在的模板默认不勾选（--only-new 时也会自动跳过）。'
            chosen = self.ui.checklist('3/6 勾选模板', text, items)
            if chosen:
                st['templates'] = chosen
                return True
            self.ui.msgbox('未选择模板', '至少需要勾选一个模板。')
            preselected = set()
            skip_existing = False

    def step_options(self):
        st = self.state
        items = [
            ('only_new', '跳过 PVE 中已存在的模板（--only-new）', st.get('only_new', True)),
            ('tuning', '注入网络/内核调优：BBR + 大缓冲（默认开启）', st.get('tuning', True)),
            ('refresh', '忽略缓存，强制重新下载镜像（--refresh）', st.get('refresh', False)),
            ('insecure', '跳过 TLS 证书校验（--insecure，仅排障用）', st.get('insecure', False)),
        ]
        chosen = set(self.ui.checklist('4/6 导入选项', '空格勾选 / 取消，回车确认：', items))
        for key, _, _ in items:
            st[key] = key in chosen

    def step_mirror(self):
        st = self.state
        text = ('可选：导入时把镜像内的软件源替换为国内/内网镜像（在 templates.yaml 的 mirrors 中配置）。\n'
                '仅影响模板内的 apt/yum/apk 源，不影响镜像文件的下载地址。')
        st['mirror'] = mirror_menu(self.ui, self.mirrors, st.get('mirror'), '5/6 软件源镜像', text)

    def step_vm(self):
        st = self.state
        vm_defaults = self.pveimport.VM_DEFAULTS
        st.setdefault('bridge', vm_defaults['bridge'])
        st.setdefault('memory', vm_defaults['memory'])
        st.setdefault('cores', vm_defaults['cores'])
        bridges = list_bridges()
        if st['bridge'] not in bridges and bridges and st['bridge'] == vm_defaults['bridge']:
            st['bridge'] = bridges[0]
        while True:
            items = [
                ('done', '继续 →'),
                ('bridge', f'网桥      : {st["bridge"]}'),
                ('memory', f'内存 (MB) : {st["memory"]}'),
                ('cores', f'CPU 核数  : {st["cores"]}'),
            ]
            text = '模板 VM 的默认硬件（克隆后随时可改）。其余固定项：virtio-scsi-single + discard、cpu=host、qemu-guest-agent。'
            choice = self.ui.menu('6/6 模板硬件', text, items, default='done')
            if choice == 'done':
                return
            if choice == 'bridge':
                if bridges:
                    bitems = [(b, '检测到的网桥') for b in bridges] + [('__manual__', '手动输入...')]
                    picked = self.ui.menu('网桥', '模板网卡 net0 所接的网桥：', bitems,
                                          default=st['bridge'] if st['bridge'] in bridges else '__manual__')
                else:
                    picked = '__manual__'
                if picked == '__manual__':
                    picked = self.ui.inputbox('网桥', '输入网桥名称（如 vmbr0）：', st['bridge'])
                if re.fullmatch(r'[A-Za-z0-9_.-]+', picked or ''):
                    st['bridge'] = picked
                else:
                    self.ui.msgbox('输入无效', '网桥名称只能包含字母、数字、点、下划线和连字符。')
            elif choice in ('memory', 'cores'):
                label = '内存大小（MB）' if choice == 'memory' else 'CPU 核数'
                value = self.ui.inputbox(label, f'输入{label}：', str(st[choice]))
                if value.isdigit() and int(value) > 0:
                    st[choice] = int(value)
                else:
                    self.ui.msgbox('输入无效', f'{label}必须是正整数。')

    def summary_text(self) -> str:
        st = self.state
        chosen = [t['name'] for t in self.templates if t['name'] in set(st['templates'])]
        new_count = len([n for n in chosen if n not in self.existing])
        opts = []
        opts.append('跳过已存在' if st.get('only_new') else '不跳过已存在')
        opts.append('调优:开' if st.get('tuning', True) else '调优:关')
        if st.get('refresh'):
            opts.append('强制重下')
        if st.get('insecure'):
            opts.append('跳过 TLS 校验')
        cols, rows = shutil.get_terminal_size((80, 24))
        lines = [
            f'存储     : {st["storage"]}',
            f'起始 VMID: {st["start_vmid"]}',
            f'模板     : {len(chosen)} 个（其中 {new_count} 个尚未导入）',
        ]
        if rows >= 34 or not isinstance(self.ui, DialogUI):
            lines += textwrap.wrap(', '.join(chosen), width=max(40, min(cols, 96) - 16),
                                   initial_indent='           ', subsequent_indent='           ')
        lines += [
            f'选项     : {", ".join(opts)}',
            f'镜像源   : {st.get("mirror") or "官方源"}',
            f'硬件     : bridge={st["bridge"]}  memory={st["memory"]}MB  cores={st["cores"]}',
            '',
            '等价命令 :',
            f'  {self.command_line()}',
        ]
        return '\n'.join(lines)

    def run(self) -> Optional[List[str]]:
        steps = [self.step_storage, self.step_vmid, self.step_templates,
                 self.step_options, self.step_mirror, self.step_vm]
        idx = 0
        while idx < len(steps):
            try:
                steps[idx]()
                idx += 1
            except Cancel:
                if idx == 0:
                    if self.ui.yesno('退出', '退出导入向导？', '退出', '继续'):
                        return None
                    if not (os.isatty(0) or isinstance(self.ui, DialogUI)):
                        return None
                else:
                    idx -= 1

        edit_items = [
            ('start', '开始导入' + ('（--dry-run，仅打印计划）' if self.dry_run else '')),
            ('storage', '修改：存储'),
            ('vmid', '修改：起始 VMID'),
            ('templates', '修改：系统模板'),
            ('options', '修改：导入选项'),
            ('mirror', '修改：软件源镜像'),
            ('vm', '修改：模板硬件'),
            ('print', '仅打印等价命令并退出'),
            ('quit', '退出'),
        ]
        editors = {
            'storage': self.step_storage, 'vmid': self.step_vmid, 'templates': self.step_templates,
            'options': self.step_options, 'mirror': self.step_mirror, 'vm': self.step_vm,
        }
        while True:
            try:
                choice = self.ui.menu('确认导入', self.summary_text(), edit_items, default='start',
                                      cancel_label='退出')
            except Cancel:
                choice = 'quit'
            if choice == 'start':
                return self.build_args()
            if choice == 'print':
                self.printed = True
                print(self.command_line())
                return None
            if choice == 'quit':
                return None
            try:
                editors[choice]()
            except Cancel:
                pass


# ---------------------------------------------------------------------------
# 更新已导入模板的向导（update-templates.py）
# ---------------------------------------------------------------------------
class UpdateWizard:
    ACTION_ITEMS = (
        ('tuning', '网络/内核调优：BBR + 大缓冲（--tuning，默认）', True),
        ('qga', '安装并启用 qemu-guest-agent（--qga，需联网）', False),
        ('permit_root', '允许 root SSH 登录（--permit-root）', False),
        ('reapply', '按 templates.yaml 重新应用该模板的 customize（--reapply）', False),
    )

    def __init__(self, ui, pveupdate, mirrors: dict, state: dict, dry_run: bool = False):
        self.ui = ui
        self.pveupdate = pveupdate
        self.mirrors = mirrors
        self.state = state
        self.dry_run = dry_run
        self.printed = False
        self.node = pveupdate.local_node_name() or ''
        vms = pveupdate.list_vms()
        if self.node:
            vms = [v for v in vms if (v.get('node') or self.node) == self.node]
        self.vms = sorted(vms, key=lambda v: v['vmid'])
        self.targets_pool = [v for v in self.vms
                             if v['template'] == 1 or v['status'] in ('stopped', '')]
        self.clones = pveupdate.linked_clone_map(self.vms) if hasattr(pveupdate, 'linked_clone_map') else {}

    def selected_vms(self) -> List[dict]:
        chosen = set(self.state.get('targets') or [])
        return [v for v in self.targets_pool if v['vmid'] in chosen]

    # ---- 步骤 ----
    def step_targets(self):
        st = self.state
        if not self.targets_pool:
            self.ui.msgbox('没有可更新的目标',
                           f'节点 {self.node or "?"} 上没有模板，也没有已停止的 VM。\n'
                           '请先用「导入新模板」创建模板。')
            raise Cancel()
        prev = set(st.get('targets') or [])
        default_all = not prev
        items = []
        for v in self.targets_pool:
            is_tpl = v['template'] == 1
            kind = '模板' if is_tpl else f'VM/{v["status"] or "stopped"}'
            cl = self.clones.get(v['vmid'], [])
            desc = f'{v["name"]:<24} [{kind}]' + (f' ⚠{len(cl)} 个链接克隆' if cl else '')
            checked = (v['vmid'] in prev) if prev else (default_all and is_tpl)
            items.append((str(v['vmid']), desc, checked))
        text = (f'节点：{self.node or "?"}    模板 {sum(1 for v in self.targets_pool if v["template"] == 1)} 个，'
                f'已停止的普通 VM {sum(1 for v in self.targets_pool if v["template"] != 1)} 个\n'
                '空格勾选 / 取消，回车确认。普通 VM 需处于停止状态才能修改其系统盘。\n'
                '带「链接克隆」的模板：修改基卷可能影响克隆出的 VM（高级选项里可选择自动跳过）。')
        while True:
            chosen = self.ui.checklist('1/4 选择要更新的模板', text, items)
            if chosen:
                st['targets'] = [int(c) for c in chosen]
                return
            self.ui.msgbox('未选择目标', '至少需要勾选一个模板或 VM。')

    def step_actions(self):
        st = self.state
        items = [(key, desc, st.get(key, default)) for key, desc, default in self.ACTION_ITEMS]
        chosen = set(self.ui.checklist('2/4 选择动作', '将对每个目标的系统盘依次执行以下动作：', items))
        for key, _, _ in self.ACTION_ITEMS:
            st[key] = key in chosen

    def step_mirror(self):
        st = self.state
        text = ('可选：把模板内的软件源替换为国内/内网镜像（按模板名识别发行版，\n'
                '不支持的发行版会跳过换源）。')
        st['mirror'] = mirror_menu(self.ui, self.mirrors, st.get('mirror'), '3/4 软件源镜像', text)

    def step_options(self):
        st = self.state
        st.setdefault('run_script', '')
        st.setdefault('tcg', False)
        st.setdefault('debug', False)
        st.setdefault('skip_cloned', False)
        st.setdefault('lock', True)
        while True:
            onoff = lambda key: '是' if st.get(key) else '否'
            items = [
                ('done', '继续 →'),
                ('run_script', f'额外脚本   : {st["run_script"] or "（无）"}'),
                ('skip_cloned', f'跳过有链接克隆的模板 --skip-cloned : {onoff("skip_cloned")}'),
                ('lock', f'修改期间给 VM 加锁（防止同时克隆/备份）: {onoff("lock")}'),
                ('tcg', f'强制软件模拟 --tcg : {onoff("tcg")}（KVM 不可用 / 嵌套虚拟化时）'),
                ('debug', f'libguestfs 调试日志 --debug : {onoff("debug")}'),
            ]
            choice = self.ui.menu('4/4 高级选项', '一般保持默认即可。', items, default='done')
            if choice == 'done':
                return
            if choice == 'run_script':
                value = self.ui.inputbox('额外脚本', '在镜像内额外执行的宿主机脚本路径（留空表示不执行）：',
                                         st['run_script'])
                if value and not os.path.exists(value if os.path.isabs(value) else os.path.join(SCRIPT_DIR, value)):
                    self.ui.msgbox('文件不存在', f'找不到脚本：{value}')
                    continue
                st['run_script'] = value
            else:
                st[choice] = not st.get(choice)

    def build_args(self) -> List[str]:
        st = self.state
        vms = self.selected_vms()
        args = [str(v['vmid']) for v in vms]
        if any(v['template'] != 1 for v in vms):
            args.append('--vms')
        if not st.get('tuning', True):
            args.append('--no-tuning')
        if st.get('qga'):
            args.append('--qga')
        if st.get('permit_root'):
            args.append('--permit-root')
        if st.get('reapply'):
            args.append('--reapply')
        if st.get('mirror'):
            args += ['--mirror', st['mirror']]
        if st.get('run_script'):
            args += ['--run', st['run_script']]
        if st.get('skip_cloned'):
            args.append('--skip-cloned')
        if not st.get('lock', True):
            args.append('--no-lock')
        if st.get('tcg'):
            args.append('--tcg')
        if st.get('debug'):
            args.append('--debug')
        if self.dry_run:
            args.append('--dry-run')
        args.append('-y')
        return args

    def command_line(self) -> str:
        return 'python3 update-templates.py ' + ' '.join(shlex.quote(a) for a in self.build_args())

    def summary_text(self) -> str:
        st = self.state
        vms = self.selected_vms()
        actions = [desc.split('（')[0] for key, desc, _ in self.ACTION_ITEMS if st.get(key, key == 'tuning')]
        if st.get('mirror'):
            actions.append(f'换源:{st["mirror"]}')
        if st.get('run_script'):
            actions.append(f'脚本:{os.path.basename(st["run_script"])}')
        cols, _ = shutil.get_terminal_size((80, 24))
        lines = [f'目标   : {len(vms)} 个（节点 {self.node or "?"}）']
        lines += textwrap.wrap(', '.join(f'{v["vmid"]}({v["name"]})' for v in vms),
                               width=max(40, min(cols, 96) - 16),
                               initial_indent='         ', subsequent_indent='         ')
        cloned = [v for v in vms if self.clones.get(v['vmid'])]
        opt_flags = [x for x in ['--skip-cloned' if st.get('skip_cloned') else '',
                                 '--no-lock' if not st.get('lock', True) else '',
                                 '--tcg' if st.get('tcg') else '', '--debug' if st.get('debug') else '',
                                 '--dry-run' if self.dry_run else ''] if x]
        lines += [
            f'动作   : {", ".join(actions) or "（无）"}',
            f'选项   : {", ".join(opt_flags) or "默认（修改期间加锁）"}',
            '',
        ]
        if cloned and not st.get('skip_cloned'):
            lines += ['⚠ 以下模板有链接克隆，修改基卷可能影响克隆出的 VM（建议先备份/快照）：']
            lines += textwrap.wrap(', '.join(f'{v["vmid"]}→{",".join(map(str, self.clones[v["vmid"]]))}' for v in cloned),
                                   width=max(40, min(cols, 96) - 16), initial_indent='  ', subsequent_indent='  ')
        else:
            lines += ['⚠ virt-customize 会就地修改模板系统盘，建议对重要模板先备份。']
        lines += [
            '',
            '等价命令 :',
            f'  {self.command_line()}',
        ]
        return '\n'.join(lines)

    def run(self) -> Optional[List[str]]:
        steps = [self.step_targets, self.step_actions, self.step_mirror, self.step_options]
        idx = 0
        while idx < len(steps):
            try:
                steps[idx]()
                idx += 1
            except Cancel:
                if idx == 0:
                    return None
                idx -= 1
        if not any(self.state.get(k, k == 'tuning') for k, _, _ in self.ACTION_ITEMS) \
                and not self.state.get('mirror') and not self.state.get('run_script'):
            self.ui.msgbox('没有动作', '至少需要选择一个动作（调优 / qga / root 登录 / 换源 / 脚本）。')
            return self.run()

        edit_items = [
            ('start', '开始更新' + ('（--dry-run，仅打印）' if self.dry_run else '')),
            ('targets', '修改：目标模板'),
            ('actions', '修改：动作'),
            ('mirror', '修改：软件源镜像'),
            ('options', '修改：高级选项'),
            ('print', '仅打印等价命令并退出'),
            ('quit', '退出'),
        ]
        editors = {'targets': self.step_targets, 'actions': self.step_actions,
                   'mirror': self.step_mirror, 'options': self.step_options}
        while True:
            try:
                choice = self.ui.menu('确认更新', self.summary_text(), edit_items, default='start', cancel_label='退出')
            except Cancel:
                choice = 'quit'
            if choice == 'start':
                return self.build_args()
            if choice == 'print':
                self.printed = True
                print(self.command_line())
                return None
            if choice == 'quit':
                return None
            try:
                editors[choice]()
            except Cancel:
                pass


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def parse_update_args(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('selectors', nargs='*')
    parser.add_argument('--all', action='store_true')
    parser.add_argument('--vms', action='store_true')
    parser.add_argument('--tuning', action='store_true', default=None)
    parser.add_argument('--no-tuning', action='store_true', default=None)
    parser.add_argument('--qga', action='store_true', default=None)
    parser.add_argument('--permit-root', action='store_true', default=None)
    parser.add_argument('--reapply', action='store_true', default=None)
    parser.add_argument('--skip-cloned', action='store_true', default=None)
    parser.add_argument('--no-lock', action='store_true', default=None)
    parser.add_argument('--mirror')
    parser.add_argument('--run')
    parser.add_argument('--tcg', action='store_true', default=None)
    parser.add_argument('--debug', action='store_true', default=None)
    parser.add_argument('--node')
    parser.add_argument('--all-nodes', action='store_true')
    parser.add_argument('-y', '--yes', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--text', action='store_true')
    parser.add_argument('--no-save', action='store_true')
    parser.add_argument('-h', '--help', action='store_true')
    return parser.parse_args(argv)


def run_update_wizard(argv):
    args = parse_update_args(argv)
    if args.help:
        print(__doc__.strip())
        return
    ensure_tty(args.text)
    ui = pick_ui(args.text)
    pveupdate = load_update_module()
    pveimport = load_import_module()
    mirrors = pveimport.load_config().get('mirrors', {}) or {}

    full_state = load_state()
    state = dict(full_state.get('update') or {})
    if args.selectors or args.all:
        vms = pveupdate.list_vms()
        picked = pveupdate.select_targets(vms, args.selectors, args.all, args.vms)
        state['targets'] = [v['vmid'] for v in picked]
    if args.no_tuning:
        state['tuning'] = False
    elif args.tuning:
        state['tuning'] = True
    for key, value in (('qga', args.qga), ('permit_root', args.permit_root), ('reapply', args.reapply),
                       ('skip_cloned', args.skip_cloned), ('tcg', args.tcg), ('debug', args.debug)):
        if value is not None:
            state[key] = value
    if args.no_lock:
        state['lock'] = False
    if args.mirror:
        state['mirror'] = args.mirror
    if args.run:
        state['run_script'] = args.run

    wizard = UpdateWizard(ui, pveupdate, mirrors, state, dry_run=args.dry_run)
    update_args = wizard.run()
    if update_args is None:
        if not wizard.printed:
            print('已取消，未做任何更改。')
        return
    if not args.no_save:
        full_state['update'] = {k: v for k, v in state.items() if k not in ('debug',)}
        save_state(full_state)
    print(f'\n==> {wizard.command_line()}\n')
    os.execv(sys.executable, [sys.executable, UPDATE_PY, *update_args])


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('storage', nargs='?')
    parser.add_argument('start_vmid', nargs='?')
    parser.add_argument('template_filter', nargs='?')
    parser.add_argument('--only-new', action='store_true', default=None)
    parser.add_argument('--refresh', action='store_true', default=None)
    parser.add_argument('--no-tuning', action='store_true', default=None)
    parser.add_argument('--insecure', action='store_true', default=None)
    parser.add_argument('--all', action='store_true', default=None)
    parser.add_argument('--mirror')
    parser.add_argument('--bridge')
    parser.add_argument('--memory', type=int)
    parser.add_argument('--cores', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--text', action='store_true', help='强制使用纯文本界面')
    parser.add_argument('--no-save', action='store_true', help='不记住本次选择')
    parser.add_argument('-h', '--help', action='store_true')
    return parser.parse_args(argv)


def choose_mode(force_text: bool) -> Optional[str]:
    """入口菜单：导入新模板 / 更新已导入的模板。"""
    ensure_tty(force_text)
    ui = pick_ui(force_text)
    items = [
        ('import', '导入新模板     下载云镜像 → 定制 → 创建 PVE 模板'),
        ('update', '更新已导入模板 对现有模板就地补调优 / qga / root 登录 / 换源'),
        ('quit', '退出'),
    ]
    try:
        choice = ui.menu('pve-import-template', f'节点：{local_node_name() or "?"}    {pve_version()}\n请选择要做的事情：',
                         items, default='import', cancel_label='退出')
    except Cancel:
        return None
    return None if choice == 'quit' else choice


def main():
    argv = sys.argv[1:]
    mode = None
    if argv and argv[0] in ('import', 'update'):
        mode = argv.pop(0)
    elif not any(a in ('-h', '--help') for a in argv) and not [a for a in argv if not a.startswith('-')]:
        mode = choose_mode('--text' in argv)
        if mode is None:
            print('已退出。')
            return
    else:
        mode = 'import'

    if mode == 'update':
        run_update_wizard(argv)
        return

    args = parse_args(argv)
    if args.help:
        print(__doc__.strip())
        return

    ensure_tty(args.text)
    pveimport = load_import_module()
    config = pveimport.load_config()
    ui = pick_ui(args.text)

    full_state = load_state()
    state = full_state
    if args.storage:
        state['storage'] = args.storage
    if args.start_vmid and str(args.start_vmid).isdigit():
        state['start_vmid'] = int(args.start_vmid)
    if args.all:
        state['preset'] = PRESET_ALL
        state['templates'] = [t['name'] for t in config.get('templates', [])]
    elif args.template_filter:
        matched = pveimport.match_templates(config.get('templates', []), args.template_filter)
        state['preset'] = PRESET_CUSTOM
        state['templates'] = [t['name'] for t in matched]
    for key, value in (('only_new', args.only_new), ('refresh', args.refresh), ('insecure', args.insecure)):
        if value is not None:
            state[key] = value
    if args.no_tuning:
        state['tuning'] = False
    if args.mirror:
        state['mirror'] = args.mirror
    if args.bridge:
        state['bridge'] = args.bridge
    if args.memory:
        state['memory'] = args.memory
    if args.cores:
        state['cores'] = args.cores

    wizard = Wizard(ui, pveimport, config, state, dry_run=args.dry_run)
    import_args = wizard.run()
    if import_args is None:
        if not wizard.printed:
            print('已取消，未做任何更改。')
        return

    if not args.no_save:
        save_state({k: v for k, v in state.items() if k != 'insecure'})

    print(f'\n==> {wizard.command_line()}\n')
    os.execv(sys.executable, [sys.executable, IMPORT_PY, *import_args])


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n已取消。')
        sys.exit(130)
