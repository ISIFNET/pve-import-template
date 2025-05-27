#!/usr/bin/env python3

import sys
import os
import contextlib
import urllib.request
import subprocess
import json

def exit_missing_dep():
    print('Some dependencies are missing.')
    print('Please run setup.sh to install them.')
    sys.exit(2)

try:
    import tqdm
    import yaml
except ImportError:
    exit_missing_dep()

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
            return f'{vmid}/vm-{vmid}-disk-0.qcow2'

    class Raw(Base):
        def format_disk_name(self, vmid: int):
            return f'vm-{vmid}-disk-0'

def run(cmd, **kwargs):
    print(f'# {cmd}')
    subprocess.run(cmd, env=kwargs, shell=isinstance(cmd, str))

def check_storage(name: str) -> StorageInfo.Base:
    output = subprocess.check_output(['pvesh', 'get', '/storage', '--output-format=json-pretty'])
    for st in json.loads(output):
        if st['storage'] != name:
            continue
        if 'images' not in st['content'].split(','):
            raise Exception(f'PVE storage {name} does not support VM images.')
        t = st['type']
        if t in ['dir','nfs','glusterfs']:
            return StorageInfo.Dir(name)
        if t in ['zfspool','lvm','lvmthin']:
            return StorageInfo.Raw(name)
        raise Exception(f'Unsupported PVE storage type {t}.')
    raise Exception(f'PVE storage {name} does not exist.')

def vm_exists_by_name(name: str) -> bool:
    try:
        out = subprocess.check_output(['qm','list'])
    except subprocess.CalledProcessError:
        return False
    for line in out.decode().splitlines()[1:]:
        cols = line.split()
        if len(cols)>=2 and cols[1]==name:
            return True
    return False

def build_customize_args(customize: dict) -> list:
    if not customize:
        return []
    args = []
    for up in customize.get('uploads', []):
        args += ['--upload', up]
    for cmd in customize.get('commands', []):
        args += ['--run-command', cmd]
    return args

def import_template(template: dict, storage: StorageInfo.Base, vmid: int):
    name = template['name']
    url  = template['url']

    if vm_exists_by_name(name):
        print(f'VM with name "{name}" exists, skipping.')
        return

    print(f'Importing VMID {vmid} ({name}) from {url}')
    os.makedirs('./cloud_img', exist_ok=True)
    dl = f'./cloud_img/{name}.img.download'
    img = f'./cloud_img/{name}.img'

    with contextlib.suppress(FileNotFoundError):
        os.remove(dl); os.remove(img)

    with DownloadProgressBar(unit='B', unit_scale=True, miniters=1) as t:
        urllib.request.urlretrieve(url, dl, reporthook=t.update_to)

    if template.get('unpack'):
        run(template['unpack'].replace('{dl}', dl).replace('{img}', img))
    else:
        os.rename(dl, img)

    cust_args = build_customize_args(template.get('customize'))
    if cust_args:
        run(['virt-customize','-a',img,*cust_args], LIBGUESTFS_BACKEND='direct')

    run(f'qm create {vmid} --name {name} --memory 512 --net0 virtio,bridge=vmbr0,queues=4 --cpu host,flags=+aes')
    run(f'qm importdisk {vmid} {img} {storage.name} -format qcow2')

    disk = storage.format_disk_name(vmid)
    run(f'qm set {vmid} --scsihw virtio-scsi-pci --scsi0 {storage.name}:{disk}')
    run(f'qm set {vmid} --boot c --bootdisk scsi0')
    run(f'qm set {vmid} --serial0 socket')

    if template.get('cloud_init'):
        run(f'qm set {vmid} --ide2 {storage.name}:cloudinit')
        run(f'qm set {vmid} --ciuser root')

    run(f'qm template {vmid}')
    print(f'Deleting {img}')
    with contextlib.suppress(FileNotFoundError):
        os.remove(img)
    print('Done\n')

def main():
    # 检查依赖
    try:
        subprocess.call(['virt-customize','--version'], stdout=subprocess.DEVNULL)
        subprocess.call(['unzip'],          stdout=subprocess.DEVNULL)
    except FileNotFoundError:
        exit_missing_dep()

    if len(sys.argv) < 3:
        print('Usage: python3 import.py <storage-name> <start-vmid> [template-name]')
        sys.exit(1)

    storage_name = sys.argv[1]
    try:
        start_vmid = int(sys.argv[2])
    except ValueError:
        print('Error: <start-vmid> must be an integer.')
        sys.exit(1)
    template_filter = sys.argv[3] if len(sys.argv)>3 else None

    storage = check_storage(storage_name)

    with open('templates.yaml') as f:
        all_tpls = yaml.safe_load(f)['templates']
    # 按名称过滤（若未指定则全量）
    to_import = [t for t in all_tpls if template_filter is None or t['name']==template_filter]

    for idx, tpl in enumerate(to_import):
        import_template(tpl, storage, start_vmid + idx)

if __name__ == '__main__':
    main()
