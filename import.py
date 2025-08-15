#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import.py - Proxmox VE Cloud Image Importer

功能亮点
- 仅导入“新增模板”：--only-new
- 选择性导入：支持逗号分隔或通配（glob），例如：'ubuntu-*'
- 下载缓存：对比 ETag/Last-Modified，无变化则跳过下载
- 支持模板自定义 unpack（占位符 {dl} / {img}）
- 离线定制：virt-customize（请在 templates.yaml 里使用你更新后的 *command_install_qga）
- 保留原有 cloud-init 选项设置
- 默认保留下载好的镜像文件；--refresh 可强制重下并覆盖缓存
- 所有外部命令失败会抛出异常（更快发现问题）

用法
  python3 import.py <storage-name> <start-vmid> [template-name[,name2|glob]] [--only-new] [--refresh]

示例
  # 全部导入（从 900 开始编号）
  python3 import.py local-lvm 900

  # 只导入 PVE 里还不存在的模板
  python3 import.py local-lvm 900 --only-new

  # 只导入指定两个模板
  python3 import.py local-lvm 900 ubuntu-22.04,ubuntu-20.04

  # 使用通配导入一批
  python3 import.py local-lvm 900 'ubuntu-*'

  # 强制刷新镜像（无视缓存，重新下载）
  python3 import.py local-lvm 900 ubuntu-22.04 --refresh
"""

import sys
import os
import contextlib
import urllib.request
import subprocess
import json
import fnmatch
from typing import Iterable, Tuple, Optional

try:
    import tqdm
    import yaml
except ImportError:
    print('Some dependencies are missing.')
    print('Please run setup.sh to install them.')
    sys.exit(2)


class DownloadProgressBar(tqdm.tqdm):
    def update_to(self, b=1, bsize=1, tsize=None):
        if tsize is not None:
            self.total = tsize
        self.update(b * bsize - self.n)


class StorageInfo:
    class Base:
        def __init__(self, name) -> None:
            self.name = name
        def format_disk_name(self, vmid: int):
            raise NotImplementedError

    class Dir(Base):
        def format_disk_name(self, vmid: int):
            # qm importdisk 默认会生成 <vmid>/vm-<vmid>-disk-0.qcow2
            return f'{vmid}/vm-{vmid}-disk-0.qcow2'

    class Raw(Base):
        def format_disk_name(self, vmid: int):
            # zfspool/lvm/lvmthin 等为块设备名
            return f'vm-{vmid}-disk-0'


def run(cmd, **extra_env):
    """
    统一外部命令执行：打印命令、继承环境、失败抛异常。
    cmd 可为 list 或 str（str 时使用 shell=True）
    """
    print(f'# {cmd}')
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    subprocess.run(
        cmd,
        check=True,
        env=env,
        shell=isinstance(cmd, str)
    )


def check_storage(name: str) -> StorageInfo.Base:
    output = subprocess.check_output(['pvesh', 'get', '/storage', '--output-format=json-pretty'])
    for st in json.loads(output):
        if st.get('storage') != name:
            continue
        content = st.get('content', '')
        if 'images' not in content.split(','):
            raise Exception(f'PVE storage {name} does not support VM images.')
        t = st.get('type')
        if t in ['dir', 'nfs', 'glusterfs']:
            return StorageInfo.Dir(name)
        if t in ['zfspool', 'lvm', 'lvmthin']:
            return StorageInfo.Raw(name)
        raise Exception(f'Unsupported PVE storage type {t}.')
    raise Exception(f'PVE storage {name} does not exist.')


def vm_exists_by_name(name: str) -> bool:
    try:
        out = subprocess.check_output(['qm', 'list'])
    except subprocess.CalledProcessError:
        return False
    for line in out.decode(errors='ignore').splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[1] == name:
            return True
    return False


def list_existing_vm_names() -> set:
    try:
        out = subprocess.check_output(['qm', 'list'])
    except subprocess.CalledProcessError:
        return set()
    names = set()
    for line in out.decode(errors='ignore').splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2:
            names.add(cols[1])
    return names


def build_customize_args(customize: Optional[dict]) -> list:
    if not customize:
        return []
    args = []
    for up in customize.get('uploads', []):
        args += ['--upload', up]
    for cmd in customize.get('commands', []):
        args += ['--run-command', cmd]
    return args


def http_head(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    尝试 HEAD，返回 (ETag, Last-Modified)。若不支持/失败则抛出异常由调用方处理。
    """
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req) as resp:
        etag = resp.headers.get("ETag")
        lm = resp.headers.get("Last-Modified")
        return etag, lm


def load_meta(meta_path: str) -> dict:
    try:
        with open(meta_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_meta(meta_path: str, data: dict):
    with open(meta_path, "w") as f:
        json.dump(data, f)


def download_with_cache(name: str, url: str, unpack_cmd: Optional[str] = None, refresh: bool = False) -> str:
    """
    下载镜像（支持缓存比较），必要时解包成 {img}。
    - name: 模板名（用于生成本地文件名）
    - url: 远端镜像/压缩包地址
    - unpack_cmd: 例如 'unzip -p {dl} > {img}' 或 'xz -dc {dl} > {img}'
                  可使用占位符 {dl}（下载文件）、{img}（目标镜像）
    - refresh: True 时忽略缓存、强制重下
    返回：img 路径
    """
    os.makedirs('./cloud_img', exist_ok=True)
    img = f'./cloud_img/{name}.img'
    dl  = f'./cloud_img/{name}.img.download'
    meta = f'./cloud_img/{name}.meta.json'

    if not refresh and os.path.exists(img):
        # 试图用 HEAD 比对
        try:
            etag_new, lm_new = http_head(url)
            meta_old = load_meta(meta)
            if etag_new and meta_old.get("etag") == etag_new:
                print(f'Image for {name} unchanged (ETag), skip download.')
                return img
            if lm_new and meta_old.get("last_modified") == lm_new:
                print(f'Image for {name} unchanged (Last-Modified), skip download.')
                return img
        except Exception as e:
            print(f'HEAD check failed: {e}, will download.')

    # 开始下载
    with contextlib.suppress(FileNotFoundError):
        os.remove(dl)
    with DownloadProgressBar(unit="B", unit_scale=True, miniters=1) as t:
        urllib.request.urlretrieve(url, dl, reporthook=t.update_to)

    # 如果需要解包
    if unpack_cmd:
        # 确保旧 img 不干扰
        with contextlib.suppress(FileNotFoundError):
            os.remove(img)
        cmd = unpack_cmd.replace('{dl}', dl).replace('{img}', img)
        run(cmd)
    else:
        # 直接移动为 img
        with contextlib.suppress(FileNotFoundError):
            os.remove(img)
        os.replace(dl, img)

    # 更新元信息
    try:
        etag, lm = http_head(url)
        save_meta(meta, {"etag": etag, "last_modified": lm})
    except Exception as e:
        print(f'HEAD after download failed: {e}')

    # 清理下载缓存文件（如果还在）
    with contextlib.suppress(FileNotFoundError):
        os.remove(dl)

    return img


def match_templates(all_tpls: list, filter_expr: Optional[str]) -> list:
    """支持逗号分隔/通配的模板选择"""
    if not filter_expr:
        return all_tpls
    names = [s.strip() for s in filter_expr.split(",") if s.strip()]
    matched = []
    for n in names:
        if any(ch in n for ch in "*?[]"):
            matched.extend([t for t in all_tpls if fnmatch.fnmatch(t["name"], n)])
        else:
            matched.extend([t for t in all_tpls if t["name"] == n])
    # 去重保持顺序
    seen = set()
    uniq = []
    for t in matched:
        if t["name"] not in seen:
            seen.add(t["name"])
            uniq.append(t)
    return uniq


def import_template(template: dict, storage: StorageInfo.Base, vmid: int, keep_image: bool = True, refresh: bool = False):
    name = template['name']
    url  = template['url']

    if vm_exists_by_name(name):
        print(f'VM with name "{name}" exists, skipping.')
        return

    print(f'Importing VMID {vmid} ({name}) from {url}')

    unpack = template.get('unpack')  # 例如：'unzip -p {dl} > {img}'
    img = download_with_cache(name, url, unpack_cmd=unpack, refresh=refresh)

    cust_args = build_customize_args(template.get('customize'))
    if cust_args:
        # 使用 direct 后端，避免某些宿主限制导致失败
        run(['virt-customize', '-a', img, *cust_args], LIBGUESTFS_BACKEND='direct')

    # 创建 VM 并导入磁盘
    run(f'qm create {vmid} --name {name} --memory 512 --net0 virtio,bridge=vmbr0,queues=4 --cpu host,flags=+aes')
    run(f'qm importdisk {vmid} {img} {storage.name} --format qcow2')

    disk = storage.format_disk_name(vmid)
    run(f'qm set {vmid} --scsihw virtio-scsi-pci --scsi0 {storage.name}:{disk}')
    run(f'qm set {vmid} --boot c --bootdisk scsi0')
    run(f'qm set {vmid} --serial0 socket')

    if template.get('cloud_init'):
        run(f'qm set {vmid} --ide2 {storage.name}:cloudinit')
        # 如需默认 root 用户，可在模板里 cloud-init 配置上传 ssh.cfg
        run(f'qm set {vmid} --ciuser root')

    run(f'qm template {vmid}')

    if not keep_image:
        print(f'Deleting {img}')
        with contextlib.suppress(FileNotFoundError):
            os.remove(img)

    print('Done\n')


def main():
    # 依赖检查（只验证存在，不强制版本）
    try:
        subprocess.call(['virt-customize', '--version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(['pvesh', 'version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(['qm', 'version'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print('Some dependencies are missing.')
        print('Please ensure virt-customize / pvesh / qm are available on this host.')
        sys.exit(2)

    if len(sys.argv) < 3:
        print('Usage: python3 import.py <storage-name> <start-vmid> [template-name[,name2|glob]] [--only-new] [--refresh]')
        sys.exit(1)

    storage_name = sys.argv[1]
    try:
        start_vmid = int(sys.argv[2])
    except ValueError:
        print('Error: <start-vmid> must be an integer.')
        sys.exit(1)

    # 解析可选参数
    template_filter = None
    only_new = False
    refresh = False
    for arg in sys.argv[3:]:
        if arg == '--only-new':
            only_new = True
        elif arg == '--refresh':
            refresh = True
        elif not arg.startswith('-'):
            template_filter = arg

    storage = check_storage(storage_name)

    with open('templates.yaml', 'r', encoding='utf-8') as f:
        all_tpls = yaml.safe_load(f)['templates']

    to_import_all = match_templates(all_tpls, template_filter)
    if only_new:
        existing = list_existing_vm_names()
        to_import_all = [t for t in to_import_all if t['name'] not in existing]

    for idx, tpl in enumerate(to_import_all):
        import_template(
            tpl,
            storage,
            start_vmid + idx,
            keep_image=not refresh,
            refresh=refresh
        )


if __name__ == '__main__':
    main()
