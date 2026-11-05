# -*- coding: utf-8 -*-
"""import.py 的真实构建路径（build_template / smoke_test），PVE / libguestfs 命令打桩。

sync 的测试用假的 build_template；这里专测构建本身——2026-11-05 第二轮审查抓到的两个 P0（整数 VMID 被 lambda
闭包捕获成 None、新模板丢 hookscript）都藏在这条路径里。
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pitlib  # noqa: E402

spec = importlib.util.spec_from_file_location('pveimport_build', os.path.join(ROOT, 'import.py'))
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)


class BuildTemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        img = os.path.join(self.tmp.name, 'up.img')
        with open(img, 'wb') as fh:
            fh.write(b'qcow2')
        self.commands = []
        self.smoke_cfg = {'net0': 'virtio,bridge=vmbr0', 'net1': 'virtio,bridge=vmbr1', 'hookscript': 'local:snippets/hook.sh'}
        self.clone_error = None

        def run_cmd(argv, timeout=None, env=None, log=None):
            argv = [str(a) for a in argv]
            self.commands.append(argv)
            if argv[:2] == ['qm', 'clone'] and '--full' in argv and argv[argv.index('--full') + 1] == '0' and self.clone_error:
                raise pitlib.CommandError('clone failed', 255, self.clone_error)
            return ''

        patches = [
            mock.patch.object(imp, 'CACHE_DIR', self.tmp.name),
            mock.patch.object(imp, 'fetch_image', lambda *a, **k: (img, {'url': 'https://x/up.img', 'sha256': 'ab',
                                                                         'checksum': {'verified': True, 'hex': 'ab'}})),
            mock.patch.object(pitlib, 'inspect_image', return_value='qcow2'),
            mock.patch.object(pitlib, 'image_sizes', return_value=(10, 5)),
            mock.patch.object(pitlib, 'ensure_space', lambda *a, **k: None),
            mock.patch.object(pitlib, 'storage_avail', return_value=None),
            mock.patch.object(pitlib, 'run_virt_customize', lambda image, args, **kw: kw['prepare']()),
            mock.patch.object(pitlib, 'run_cmd', side_effect=run_cmd),
            mock.patch.object(pitlib, 'vm_config', lambda node, vmid: dict(self.smoke_cfg)),
            mock.patch.object(pitlib, 'local_node_name', return_value='pve1'),
            mock.patch.object(imp.subprocess, 'run'),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def build(self, vmid, **kw):
        tpl = {'name': 'debian-13', 'url': 'https://x/up.img', 'cloud_init': True}
        return imp.build_template(tpl, 'local-lvm', vmid, vm_name='debian-13-pitbuild', log=lambda m: None, **kw)

    def test_integer_vmid_is_used_for_qm_create(self):
        # P0：lambda: vmid 捕获的是变量，后面 vmid = None → qm create None，所有 import 都在最后一步失败
        meta = self.build(9042)
        create = [c for c in self.commands if c[:2] == ['qm', 'create']][0]
        self.assertEqual(create[2], '9042')
        self.assertEqual(meta['vmid'], 9042)

    def test_callable_vmid_and_collision_retry(self):
        ids = iter([9050, 9051])
        orig = pitlib.run_cmd.side_effect

        def run_cmd(argv, **kw):
            if [str(a) for a in argv[:3]] == ['qm', 'create', '9050']:
                raise pitlib.CommandError('exists', 255, 'VM 9050 already exists on node pve2')
            return orig(argv, **kw)
        pitlib.run_cmd.side_effect = run_cmd
        meta = self.build(lambda: next(ids))
        self.assertEqual(meta['vmid'], 9051)

    def test_hookscript_and_policy_keys_are_carried_into_the_new_template(self):
        meta = self.build(9042, extra_config={'hookscript': 'local:snippets/hook.sh', 'vga': 'serial0', 'args': '-evil'})
        create = [c for c in self.commands if c[:2] == ['qm', 'create']][0]
        self.assertIn('--hookscript', create)
        self.assertEqual(create[create.index('--hookscript') + 1], 'local:snippets/hook.sh')
        self.assertNotIn('--args', create)
        self.assertEqual(meta['carried'], ['hookscript', 'vga'])

    def test_disk_format_is_passed_to_import_from(self):
        self.build(9042, disk_format='qcow2')
        scsi = [c for c in self.commands if '--scsi0' in c][0]
        self.assertTrue(scsi[scsi.index('--scsi0') + 1].endswith(',format=qcow2'))

    def test_smoke_vm_has_no_network_and_no_hookscript_and_is_destroyed(self):
        self.build(9042, smoke_timeout=60, smoke_allocate=lambda: 9999)
        sets = [c for c in self.commands if c[:3] == ['qm', 'set', '9999']]
        self.assertTrue(sets)
        self.assertIn('--delete', sets[0])
        self.assertEqual(sets[0][sets[0].index('--delete') + 1], 'net0,net1,hookscript')
        self.assertGreaterEqual(int(sets[0][sets[0].index('--memory') + 1]), 1024)
        destroys = [c.args[0] for c in imp.subprocess.run.call_args_list if c.args and c.args[0][:2] == ['qm', 'destroy']]
        self.assertIn(['qm', 'destroy', '9999', '--purge', '1', '--skiplock', '1'], destroys)

    def test_smoke_falls_back_to_full_clone_only_when_linked_clones_are_unsupported(self):
        self.clone_error = 'Linked clone feature is not supported for drive scsi0'
        self.build(9042, smoke_timeout=60, smoke_allocate=lambda: 9999)
        clones = [c for c in self.commands if c[:2] == ['qm', 'clone']]
        self.assertEqual([c[c.index('--full') + 1] for c in clones], ['0', '1'])

        self.commands.clear()
        self.clone_error = "can't lock file '/var/lock/qemu-server/lock-9042.conf' - got timeout"
        with self.assertRaises(pitlib.CommandError):
            self.build(9043, smoke_timeout=60, smoke_allocate=lambda: 9998)
        self.assertEqual(len([c for c in self.commands if c[:2] == ['qm', 'clone']]), 1)
        # 冒烟失败 = 构建失败：半成品模板被清理
        self.assertTrue(any(c.args and c.args[0][:3] == ['qm', 'destroy', '9043'] for c in imp.subprocess.run.call_args_list))

    def test_customized_image_must_keep_its_format(self):
        formats = iter(['qcow2', 'raw'])
        with mock.patch.object(pitlib, 'inspect_image', side_effect=lambda p: next(formats)):
            with self.assertRaises(pitlib.PitError):
                self.build(9042)
        self.assertFalse([c for c in self.commands if c[:2] == ['qm', 'create']])


class DownloadSafetyTest(unittest.TestCase):
    def test_https_to_http_redirect_is_refused(self):
        class Resp:
            headers = {'Content-Length': '4'}

            def geturl(self):
                return 'http://mirror.example/img'

            def read(self, n=-1):
                return b''

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with tempfile.TemporaryDirectory() as d, mock.patch.object(pitlib, 'http_open', return_value=Resp()):
            with self.assertRaises(imp.DownloadError) as ctx:
                imp.download_file('x', 'https://upstream.example/img', os.path.join(d, 'img'))
            self.assertEqual(ctx.exception.kind, 'tls')


if __name__ == '__main__':
    unittest.main()
