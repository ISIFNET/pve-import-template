#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update-templates.py - 对「已经导入 PVE 的虚拟机模板」就地重新应用定制

适用场景
- 你之前用 import.py 导入过一批模板，现在想给它们补上（或更新）网络/内核调优、
  qemu-guest-agent、允许 root 登录等设置，但不想重新下载镜像、重建模板。

工作原理
- 找到目标模板 VM → 解析其系统盘卷 → 用 `pvesm path` 得到磁盘文件/块设备路径
  → 直接对该磁盘运行 virt-customize 注入脚本（与 import.py 共用 ./uploads/*.sh）。
- 不改动 VM 配置、不改 VMID、不动 cloud-init 盘，只修改系统盘内的文件。

可复用脚本（位于 ./uploads/，与 import.py 单一数据源）
- apply-net-tuning.sh    BBR + 网络/内核 sysctl 调优（默认动作）
- install-qga.sh         安装并启用 qemu-guest-agent
- permit-root-login.sh   允许 root SSH 登录

用法
  python3 update-templates.py [选择器 ...] [动作] [选项]

选择器（可混用，留空时必须配合 --all）
  <vmid>            指定 VMID，如 9000
  <name|glob>       按模板名匹配，支持通配，如 'ubuntu-*'
  --all             选择所有「模板」VM

动作（不指定动作时，默认仅执行「网络调优」）
  --tuning          应用网络/内核调优（默认开启；用 --no-tuning 关闭）
  --no-tuning       不应用网络/内核调优
  --qga             安装并启用 qemu-guest-agent
  --permit-root     允许 root SSH 登录
  --run <script>    运行任意宿主机脚本（可重复）

选项
  --vms             允许选中非模板的普通 VM（必须处于 stopped 状态）
  --node <name>     指定作用的 PVE 节点（默认自动识别为本节点）
  --all-nodes       放开节点限制（危险：对非本节点的 VM 执行会失败）
  --dry-run         只打印将执行的操作，不真正修改
  -y, --yes         跳过确认提示
  --list            列出（本节点）所有模板及其系统盘后退出
  -h, --help        显示帮助

多节点集群
  virt-customize / qm / pvesm 只能操作「本节点」的磁盘。集群里 pvesh 会返回所有节点的
  VM，因此本工具默认只处理「本节点」的模板；要更新其他节点的模板，请到对应节点上分别运行。

示例
  python3 update-templates.py --list
  python3 update-templates.py --all                       # 给所有模板补网络调优
  python3 update-templates.py --all --qga --permit-root
  python3 update-templates.py 'ubuntu-*' --dry-run
  python3 update-templates.py 9000 9001 --no-tuning --qga

⚠ 重要提示
- virt-customize 会「就地」修改模板系统盘。若该模板已被「链接克隆（linked clone，
  常见于 lvmthin/zfs）」，修改基卷可能影响这些克隆，请谨慎；建议对重要模板先做备份/快照。
- --qga 等需要联网安装软件的动作要求宿主机可访问软件源（或配合内网源）。
"""

import os
import sys
import json
import fnmatch
import subprocess
from typing import Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOADS = os.path.join(SCRIPT_DIR, 'uploads')

TUNING_SCRIPT = os.path.join(UPLOADS, 'apply-net-tuning.sh')
QGA_SCRIPT = os.path.join(UPLOADS, 'install-qga.sh')
PERMIT_ROOT_SCRIPT = os.path.join(UPLOADS, 'permit-root-login.sh')

# qm config 中代表磁盘的前缀；EFI / TPM / cloudinit / cdrom 会被跳过
DISK_PREFIXES = ('scsi', 'virtio', 'sata', 'ide')


def local_node_name() -> Optional[str]:
    """获取当前 PVE 节点名（用于把操作限制在本节点）。

    virt-customize / qm 只能操作「本节点」的磁盘；集群里 pvesh 会返回所有节点的 VM，
    直接对其他节点的 VM 执行会失败（无权限/找不到磁盘）。因此默认只处理本节点。
    """
    # 1) 最权威：/etc/pve/local 是指向 /etc/pve/nodes/<nodename> 的符号链接
    try:
        return os.path.basename(os.readlink('/etc/pve/local').rstrip('/')) or None
    except OSError:
        pass
    # 2) 回退：短主机名（PVE 节点名即短主机名）
    try:
        out = subprocess.check_output(['hostname'], stderr=subprocess.DEVNULL)
        return out.decode(errors='ignore').strip().split('.')[0] or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def run(cmd, dry_run: bool = False, **extra_env):
    print(f'# {cmd}')
    if dry_run:
        return
    env = os.environ.copy()
    env.update(extra_env)
    subprocess.run(cmd, check=True, env=env, shell=isinstance(cmd, str))


def list_vms() -> list:
    """返回 qemu VM 列表：[{vmid,name,template,status,node}]。优先用 pvesh，失败回退 qm list。"""
    try:
        out = subprocess.check_output(
            ['pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'])
        vms = []
        for r in json.loads(out):
            if r.get('type') != 'qemu':
                continue
            vms.append({
                'vmid': int(r.get('vmid')),
                'name': r.get('name', ''),
                'template': int(r.get('template', 0) or 0),
                'status': r.get('status', ''),
                'node': r.get('node', ''),
            })
        return vms
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError) as e:
        print(f'Warning: pvesh failed ({e}); falling back to `qm list`.')

    # 回退：qm list 不带模板标记，需要逐个 qm config 判断
    try:
        out = subprocess.check_output(['qm', 'list'])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    vms = []
    for line in out.decode(errors='ignore').splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 3:
            try:
                vmid = int(cols[0])
            except ValueError:
                continue
            vms.append({'vmid': vmid, 'name': cols[1], 'template': _is_template(vmid),
                        'status': cols[2], 'node': ''})
    return vms


def _is_template(vmid: int) -> int:
    cfg = get_config(vmid)
    return 1 if cfg.get('template', '').strip() in ('1', 'true', 'yes') else 0


def get_config(vmid: int) -> dict:
    try:
        out = subprocess.check_output(['qm', 'config', str(vmid)])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {}
    cfg = {}
    for line in out.decode(errors='ignore').splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            cfg[k.strip()] = v.strip()
    return cfg


def find_os_disk_volid(cfg: dict) -> Optional[str]:
    """从 qm config 中挑出系统盘卷 ID（跳过 cloudinit / cdrom / efi / tpm）。"""
    # 候选磁盘键
    candidates = []
    for key, val in cfg.items():
        if not key.startswith(DISK_PREFIXES):
            continue
        # 跳过非纯数字结尾之外的键（如 scsihw）
        suffix = key[len(next(p for p in DISK_PREFIXES if key.startswith(p))):]
        if not suffix.isdigit():
            continue
        if 'media=cdrom' in val or 'cloudinit' in val or val.split(',')[0].endswith('none'):
            continue
        volid = val.split(',')[0].strip()
        if not volid or ':' not in volid:
            continue
        candidates.append((key, volid))

    if not candidates:
        return None

    # 优先使用启动顺序里的第一个磁盘
    order = ''
    if cfg.get('boot', '').startswith('order='):
        order = cfg['boot'][len('order='):]
    elif cfg.get('bootdisk'):
        order = cfg['bootdisk']
    for dev in [d for d in order.replace(',', ';').split(';') if d]:
        for key, volid in candidates:
            if key == dev:
                return volid

    # 否则取第一个（按 scsi0/virtio0 排序）
    candidates.sort(key=lambda kv: kv[0])
    return candidates[0][1]


def resolve_disk_path(volid: str) -> Optional[str]:
    try:
        out = subprocess.check_output(['pvesm', 'path', volid])
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f'Warning: `pvesm path {volid}` failed: {e}')
        return None
    path = out.decode(errors='ignore').strip()
    return path or None


def lvm_lv_from_path(path: str) -> Optional[str]:
    """把 /dev/<vg>/<lv> 形式的路径解析为 'vg/lv'（供 lvchange 使用）；否则返回 None。"""
    parts = path.split('/')
    # ['', 'dev', '<vg>', '<lv>']
    if len(parts) == 4 and parts[1] == 'dev' and parts[2] not in ('mapper', 'zvol'):
        return f'{parts[2]}/{parts[3]}'
    return None


def lv_activate(lv: str, dry_run: bool):
    # -K 忽略「activation skip」标志：PVE 的模板基卷默认带该标志，普通 -ay 不会激活
    run(['lvchange', '-ay', '-K', lv], dry_run=dry_run)


def lv_deactivate(lv: str, dry_run: bool):
    run(['lvchange', '-an', lv], dry_run=dry_run)


def select_targets(vms: list, selectors: list, want_all: bool, include_vms: bool) -> list:
    pool = vms if include_vms else [v for v in vms if v['template'] == 1]
    if want_all:
        return pool
    selected = []
    seen = set()
    for sel in selectors:
        matched = []
        if sel.isdigit():
            vmid = int(sel)
            matched = [v for v in pool if v['vmid'] == vmid]
        elif any(ch in sel for ch in '*?[]'):
            matched = [v for v in pool if fnmatch.fnmatch(v['name'], sel)]
        else:
            matched = [v for v in pool if v['name'] == sel]
        if not matched:
            print(f'Warning: selector "{sel}" matched no '
                  f'{"VM" if include_vms else "template"}.')
        for v in matched:
            if v['vmid'] not in seen:
                seen.add(v['vmid'])
                selected.append(v)
    return selected


def build_actions(apply_tuning: bool, qga: bool, permit_root: bool, extra_runs: list) -> list:
    """返回 (label, virt-customize 参数列表) 的有序动作清单。"""
    actions = []
    if permit_root:
        actions.append(('permit-root', ['--run', PERMIT_ROOT_SCRIPT]))
    if qga:
        actions.append(('install-qga', ['--run', QGA_SCRIPT]))
    if apply_tuning:
        actions.append(('net-tuning', ['--run', TUNING_SCRIPT]))
    for script in extra_runs:
        path = script if os.path.isabs(script) else os.path.join(SCRIPT_DIR, script)
        actions.append((f'run:{os.path.basename(path)}', ['--run', path]))
    return actions


def parse_args(argv):
    selectors = []
    want_all = False
    include_vms = False
    apply_tuning = True
    qga = False
    permit_root = False
    extra_runs = []
    dry_run = False
    assume_yes = False
    do_list = False
    node = None
    all_nodes = False

    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ('-h', '--help'):
            print(__doc__)
            sys.exit(0)
        elif a == '--all':
            want_all = True
        elif a == '--node':
            if i + 1 >= len(argv):
                print('Error: --node requires a node name.')
                sys.exit(1)
            node = argv[i + 1]
            i += 1
        elif a == '--all-nodes':
            all_nodes = True
        elif a == '--vms':
            include_vms = True
        elif a == '--tuning':
            apply_tuning = True
        elif a == '--no-tuning':
            apply_tuning = False
        elif a == '--qga':
            qga = True
        elif a == '--permit-root':
            permit_root = True
        elif a == '--run':
            if i + 1 >= len(argv):
                print('Error: --run requires a script path.')
                sys.exit(1)
            extra_runs.append(argv[i + 1])
            i += 1
        elif a == '--dry-run':
            dry_run = True
        elif a in ('-y', '--yes'):
            assume_yes = True
        elif a == '--list':
            do_list = True
        elif a.startswith('-'):
            print(f'Error: unknown option "{a}".')
            sys.exit(1)
        else:
            selectors.append(a)
        i += 1

    return dict(selectors=selectors, want_all=want_all, include_vms=include_vms,
                apply_tuning=apply_tuning, qga=qga, permit_root=permit_root,
                extra_runs=extra_runs, dry_run=dry_run, assume_yes=assume_yes,
                do_list=do_list, node=node, all_nodes=all_nodes)


def print_list(vms: list, scope: str = ''):
    templates = [v for v in vms if v['template'] == 1]
    if not templates:
        print(f'未发现任何模板 VM{("（" + scope + "）") if scope else ""}。')
        return
    print(f'已有模板{("（" + scope + "）") if scope else ""}：')
    for v in sorted(templates, key=lambda x: x['vmid']):
        cfg = get_config(v['vmid'])
        volid = find_os_disk_volid(cfg)
        disk = volid or '(未识别系统盘)'
        node = f" node={v['node']}" if v.get('node') else ''
        print(f"  {v['vmid']:>6}  {v['name']:<24}{node} disk={disk}")


def main():
    opts = parse_args(sys.argv[1:])

    # 依赖检查
    try:
        subprocess.call(['virt-customize', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(['qm', 'version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print('缺少依赖：请确保 virt-customize / qm / pvesm 可用（在 PVE 宿主机上运行）。')
        sys.exit(2)

    vms = list_vms()

    # 限定作用范围：默认只处理「本节点」的 VM（virt-customize/qm 只能操作本节点磁盘）。
    # --node <name> 指定节点；--all-nodes 显式放开（仅当你确认全部 VM 都在本节点时才有意义）。
    local = opts['node'] or local_node_name()
    if opts['all_nodes']:
        node_vms, scope = vms, '所有节点'
        print('注意：--all-nodes 已启用；对非本节点的 VM 执行会失败，请仅在确知的情况下使用。')
    elif local:
        node_vms = [v for v in vms if (v.get('node') or local) == local]
        scope = f'节点 {local}'
    else:
        node_vms, scope = vms, '未能确定本节点，未过滤'
        print('Warning: 无法确定本节点名（非 PVE 环境？），未按节点过滤。')

    if opts['do_list']:
        print_list(node_vms, scope)
        return

    if not opts['selectors'] and not opts['want_all']:
        print('未指定目标。请用 VMID/名称/通配选择，或使用 --all。（--list 查看模板，--help 查看帮助）')
        sys.exit(1)

    actions = build_actions(opts['apply_tuning'], opts['qga'], opts['permit_root'], opts['extra_runs'])
    if not actions:
        print('没有要执行的动作（用了 --no-tuning 却没指定 --qga/--permit-root/--run）。')
        sys.exit(1)

    # 校验所用脚本存在
    for label, args in actions:
        for j, tok in enumerate(args):
            if tok == '--run':
                spath = args[j + 1]
                if not os.path.exists(spath):
                    print(f'Error: 脚本不存在：{spath}')
                    sys.exit(1)

    targets = select_targets(node_vms, opts['selectors'], opts['want_all'], opts['include_vms'])

    # 对「明确按 VMID 指定、但其实位于其他节点」的选择器给出可执行的提示
    if not opts['all_nodes'] and local:
        picked = {v['vmid'] for v in targets}
        for sel in opts['selectors']:
            if sel.isdigit():
                vid = int(sel)
                remote = [v for v in vms if v['vmid'] == vid and (v.get('node') or local) != local]
                if remote and vid not in picked:
                    print(f'提示：VM {vid} 位于节点 {remote[0].get("node") or "?"}，'
                          f'请在该节点上运行本工具（virt-customize 只能修改本节点磁盘）。')

    if not targets:
        print(f'没有匹配到任何目标（作用范围：{scope}）。')
        sys.exit(1)

    action_labels = ', '.join(label for label, _ in actions)
    print(f'\n[作用范围：{scope}] 将对以下 {len(targets)} 个目标应用动作 [{action_labels}]：')
    for v in targets:
        kind = '模板' if v['template'] == 1 else f"VM({v['status']})"
        print(f"  {v['vmid']:>6}  {v['name']:<24} [{kind}]")
    print('\n⚠ virt-customize 会就地修改系统盘；若存在链接克隆请谨慎，建议先备份。\n')

    if not opts['dry_run'] and not opts['assume_yes']:
        try:
            ans = input('确认继续？输入 yes 执行：').strip().lower()
        except EOFError:
            ans = ''
        if ans not in ('y', 'yes'):
            print('已取消。')
            return

    ok, failed, skipped = 0, 0, 0
    for v in targets:
        vmid, name = v['vmid'], v['name']
        print(f'\n=== {vmid} ({name}) ===')

        # 普通 VM 必须 stopped
        if v['template'] != 1 and v['status'] not in ('stopped', ''):
            print(f'  跳过：非模板 VM 且未停止（status={v["status"]}）。')
            skipped += 1
            continue

        cfg = get_config(vmid)
        if not cfg:
            print('  跳过：无法读取 qm config。')
            skipped += 1
            continue

        volid = find_os_disk_volid(cfg)
        if not volid:
            print('  跳过：未能识别系统盘卷。')
            skipped += 1
            continue

        path = resolve_disk_path(volid)
        if not path:
            print(f'  跳过：无法解析磁盘路径（volid={volid}）。')
            skipped += 1
            continue

        print(f'  系统盘：{volid} -> {path}')

        # LVM/LVM-thin：模板基卷默认未激活（带 activation-skip 标志），
        # /dev/<vg>/<lv> 设备节点此时不存在，需先激活、用完再恢复未激活状态。
        activated_lv = None
        if not os.path.exists(path):
            lv = lvm_lv_from_path(path)
            if lv:
                print(f'  卷未激活，激活 LVM 卷 {lv} ...')
                try:
                    lv_activate(lv, opts['dry_run'])
                    activated_lv = lv
                except (subprocess.CalledProcessError, FileNotFoundError) as e:
                    print(f'  跳过：激活 LVM 卷失败（{e}）。')
                    skipped += 1
                    continue
                if not opts['dry_run'] and not os.path.exists(path):
                    print(f'  跳过：激活后仍找不到设备 {path}。')
                    try:
                        lv_deactivate(lv, opts['dry_run'])
                    except (subprocess.CalledProcessError, FileNotFoundError):
                        pass
                    skipped += 1
                    continue
            elif not opts['dry_run']:
                print(f'  跳过：磁盘路径不存在且非 LVM 卷：{path}')
                skipped += 1
                continue

        va = []
        for _, args in actions:
            va += args
        try:
            run(['virt-customize', '-a', path, *va],
                dry_run=opts['dry_run'], LIBGUESTFS_BACKEND='direct')
            ok += 1
        except subprocess.CalledProcessError as e:
            print(f'  失败：virt-customize 返回非零（{e.returncode}）。')
            failed += 1
        finally:
            # 恢复模板基卷的「未激活」默认状态（仅当本工具激活过它）
            if activated_lv:
                try:
                    lv_deactivate(activated_lv, opts['dry_run'])
                except (subprocess.CalledProcessError, FileNotFoundError):
                    print(f'  警告：恢复未激活状态失败：lvchange -an {activated_lv}')

    print(f'\n完成：成功 {ok}，失败 {failed}，跳过 {skipped}'
          f'{"（dry-run，未真正修改）" if opts["dry_run"] else ""}。')


if __name__ == '__main__':
    main()
