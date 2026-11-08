#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
manifest.py —— 把 templates.yaml 整理成一份模板清单（JSON，写到 stdout）：平台 vendoring 本仓库时据此生成
templates.manifest.json（下单页的系统列表 / 显示名 / 支持期 / CPU 要求都从这里来，不必解析 yaml）。

  python3 manifest.py > templates.manifest.json

输出结构：
  {
    "tool_version": "2.2.0",
    "default_profile": "standard",
    "profiles": {"minimal": ["debian-13", ...], ...},        配置组展开后的模板名（保持 templates.yaml 顺序）
    "templates": [
      {"name": "debian-13", "family": "debian", "family_label": "Debian", "version": "13",
       "display_name": "Debian 13", "eol": "2030-06-30", "min_cpu_level": null, "retired": false},
      ...
      {"name": "debian-11", ..., "eol": true, "retired": true}             templates.yaml 的 retired 列表（不再获取）
    ]
  }

- version：模板名末尾的版本号（ubuntu-24.04 → 24.04，archLinux 没有 → null）
- display_name：默认「系列显示名 + 版本」（Debian 13 / CentOS Stream 10 / Alpine Linux 3.24 / Arch Linux），
  yaml 条目写了 display_name 就用它
- eol："YYYY-MM-DD"（到这天停止安全支持）/ true（已 EOL）/ null（没写）
- min_cpu_level：宿主机至少要 x86-64-v<N>（EL10 = 3），没要求为 null
- retired：true = 已 EOL、不再获取（不构建、不维护）；平台只拿它的显示名与 EOL 标记，节点上的同名模板算「不再维护」
只读 templates.yaml，不碰 PVE；告警走 stderr。退出码：0 成功 / 2 依赖缺失或 yaml 有误。
"""

import datetime
import importlib.util
import json
import os
import re
import sys
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


def load_import_module():
    """加载 import.py（文件名与关键字冲突，不能直接 import）：系列推断 / 配置组展开与工具本身共用一份实现。"""
    spec = importlib.util.spec_from_file_location('pveimport', os.path.join(SCRIPT_DIR, 'import.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def template_version(name: str) -> Optional[str]:
    m = re.search(r'(\d+(?:\.\d+)*)$', str(name or ''))
    return m.group(1) if m else None


def eol_value(value):
    """yaml 的 eol → "YYYY-MM-DD" / true / null（false 与没写一样是 null；不加引号的日期被 YAML 解析成 date 也认）。"""
    if value is True:
        return True
    if value is None or value is False or value == '':
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.strftime('%Y-%m-%d')
    return str(value)


def build_manifest(imp, config: dict) -> dict:
    tpls = config.get('templates') or []
    profiles = {}
    for profile in config.get('profiles') or {}:
        profiles[profile] = [t['name'] for t in imp.match_templates(tpls, '@' + profile, config=config)]
    templates = []
    for t, retired in [(t, False) for t in tpls] + [(t, True) for t in imp.retired_templates(config)]:
        family = imp.template_family(t)
        label = imp.family_label(family)
        version = template_version(t['name'])
        level = t.get('min_cpu_level')
        templates.append({
            'name': t['name'],
            'family': family,
            'family_label': label,
            'version': version,
            'display_name': str(t.get('display_name') or ('%s %s' % (label, version) if version else label)),
            'eol': True if retired else eol_value(t.get('eol')),
            'min_cpu_level': int(level) if level not in (None, '') else None,
            'retired': retired,
        })
    return {'tool_version': imp.TOOL_VERSION, 'default_profile': imp.default_profile(config),
            'profiles': profiles, 'templates': templates}


def main() -> int:
    imp = load_import_module()
    try:
        manifest = build_manifest(imp, imp.load_config())
    except (imp.pitlib.PitError, ValueError, TypeError, KeyError) as e:
        print('Error: %s' % e, file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=1, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
