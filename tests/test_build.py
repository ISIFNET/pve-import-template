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
        meta = self.build(9042, extra_config={'hookscript': 'local:snippets/hook.sh', 'vga': 'serial0',
                                              'args': '-smbios type=1,serial=ds=nocloud;s=http://x/'})
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


class BuildV22Test(BuildTemplateTest):
    """2.2.0：撞号 / 残留卷、ostype / scsihw 沿用、冒烟前内存、深度冒烟。"""

    def setUp(self):
        super().setUp()
        self.volumes = {}            # vmid -> pvesm list 输出里的卷（模拟存储）
        self.exec_replies = []       # qm guest exec 依次返回的 JSON（或异常）
        self.import_error = {}       # vmid -> qm set --scsi0 的报错输出
        orig = pitlib.run_cmd.side_effect

        def run_cmd(argv, timeout=None, env=None, log=None):
            argv = [str(a) for a in argv]
            if argv[:2] == ['pvesm', 'list']:
                self.commands.append(argv)
                vid = int(argv[argv.index('--vmid') + 1])
                return 'Volid Format Type Size VMID\n' + ''.join('%s raw images 1 %d\n' % (v, vid)
                                                                 for v in self.volumes.get(vid, []))
            if argv[:3] == ['qm', 'guest', 'exec']:
                self.commands.append(argv)
                reply = self.exec_replies.pop(0)
                if isinstance(reply, Exception):
                    raise reply
                return reply
            if argv[:2] == ['qm', 'set'] and '--scsi0' in argv and int(argv[2]) in self.import_error:
                self.commands.append(argv)
                vid = int(argv[2])
                self.volumes.setdefault(vid, []).append('local-lvm:vm-%d-disk-0' % vid)   # import 写了一半
                raise pitlib.CommandError('qm set failed', 255, self.import_error[vid])
            return orig(argv, timeout=timeout, env=env, log=log)
        pitlib.run_cmd.side_effect = run_cmd
        p = mock.patch.object(imp.time, 'sleep')
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def exec_json(code, out=''):
        import json
        return json.dumps({'exitcode': code, 'exited': 1, 'out-data': out})

    def test_file_exists_while_allocating_volumes_retries_with_another_vmid(self):
        ids = iter([9050, 9051])
        self.import_error[9050] = "lvcreate 'pve/vm-9050-cloudinit' error: Logical volume already exists"
        meta = self.build(lambda: next(ids))
        self.assertEqual(meta['vmid'], 9051)
        destroys = [c.args[0] for c in imp.subprocess.run.call_args_list if c.args[0][:2] == ['qm', 'destroy']]
        self.assertIn(['qm', 'destroy', '9050', '--purge', '1', '--skiplock', '1'], destroys)
        # import 写了一半的卷是这次才出现的、名字是 vm-9050-*：删掉
        self.assertIn(['pvesm', 'free', 'local-lvm:vm-9050-disk-0'], self.commands)

    def test_vmid_with_foreign_volumes_on_the_storage_is_not_used(self):
        ids = iter([9050, 9051])
        self.volumes[9050] = ['local-lvm:vm-9050-cloudinit']    # 共挂存储的另一台机器的
        meta = self.build(lambda: next(ids))
        self.assertEqual(meta['vmid'], 9051)
        self.assertFalse([c for c in self.commands if c[:3] == ['qm', 'create', '9050']])
        self.assertFalse([c for c in self.commands if c[:2] == ['pvesm', 'free']])

    def test_failed_build_frees_only_its_own_new_volumes(self):
        self.volumes[9042] = ['local-lvm:vm-9042-disk-7']        # 建之前就在（不是我们的）
        self.import_error[9042] = 'import failed: no space left on device'
        with self.assertRaises(pitlib.CommandError):
            self.build(9042)
        frees = [c for c in self.commands if c[:2] == ['pvesm', 'free']]
        self.assertEqual(frees, [['pvesm', 'free', 'local-lvm:vm-9042-disk-0']])

    def test_ostype_and_scsihw_replace_the_defaults(self):
        meta = self.build(9042, extra_config={'ostype': 'l24', 'scsihw': 'virtio-scsi-pci', 'numa': '1'})
        create = [c for c in self.commands if c[:2] == ['qm', 'create']][0]
        self.assertEqual(create.count('--ostype'), 1)
        self.assertEqual(create[create.index('--ostype') + 1], 'l24')
        self.assertEqual(create[create.index('--numa') + 1], '1')
        self.assertNotIn('--scsihw', create)
        scsi = [c for c in self.commands if '--scsi0' in c][0]
        self.assertEqual(scsi[scsi.index('--scsihw') + 1], 'virtio-scsi-pci')
        self.assertEqual(meta['carried'], ['numa', 'ostype', 'scsihw'])

    def test_low_memory_before_smoke_defers_and_cleans_up(self):
        with mock.patch.object(pitlib, 'mem_available_mb', return_value=1000):
            with self.assertRaises(pitlib.Deferred):
                self.build(9042, smoke_timeout=60, smoke_allocate=lambda: 9999, min_free_mem_mb=4096)
        self.assertFalse([c for c in self.commands if c[:2] == ['qm', 'clone']])
        self.assertTrue(any(c.args[0][:3] == ['qm', 'destroy', '9042'] for c in imp.subprocess.run.call_args_list))

    def deep(self):
        return self.build(9042, smoke_timeout=600, smoke_allocate=lambda: 9999, smoke_deep=True)

    def test_deep_smoke_waits_for_cloud_init_then_checks_ssh(self):
        self.exec_replies = [self.exec_json(0, 'status: running\n'), self.exec_json(2, 'status: done\n'),
                             self.exec_json(0)]
        meta = self.deep()
        self.assertNotIn('warnings', meta)
        execs = [c for c in self.commands if c[:3] == ['qm', 'guest', 'exec']]
        self.assertEqual(execs[0][-2:], ['cloud-init', 'status'])
        self.assertEqual(execs[-1][-3:-1], ['sh', '-c'])
        self.assertIn('sshd', execs[-1][-1])

    def test_deep_smoke_cloud_init_error_fails_the_build(self):
        self.exec_replies = [self.exec_json(1, 'status: error\n')]
        with self.assertRaises(pitlib.PitError) as ctx:
            self.deep()
        self.assertIn('cloud-init finished with an error: status: error', str(ctx.exception))
        self.assertTrue(any(c.args[0][:3] == ['qm', 'destroy', '9042'] for c in imp.subprocess.run.call_args_list))

    def test_deep_smoke_cloud_init_still_running_at_the_deadline_fails(self):
        self.exec_replies = [self.exec_json(0, 'status: running\n')] * 50
        clock = iter(range(0, 100000, 100))
        with mock.patch.object(imp.time, 'time', side_effect=lambda: next(clock)):
            with self.assertRaises(pitlib.PitError) as ctx:
                self.deep()
        self.assertIn('cloud-init still running', str(ctx.exception))

    def test_deep_smoke_ssh_failures_name_the_failing_check(self):
        self.exec_replies = [self.exec_json(0, 'status: done\n')] + [self.exec_json(13, '/etc/ssh/sshd_config line 3: Bad')] * 3
        with self.assertRaises(pitlib.PitError) as ctx:
            self.deep()
        self.assertIn('sshd -t rejects the configuration (exit 13)', str(ctx.exception))
        self.exec_replies = [self.exec_json(0, 'status: done\n')] + [self.exec_json(11)] * 3
        with self.assertRaises(pitlib.PitError) as ctx:
            self.build(9043, smoke_timeout=600, smoke_allocate=lambda: 9998, smoke_deep=True)
        self.assertIn('SSH host keys were not generated', str(ctx.exception))

    def test_deep_smoke_without_exec_support_is_a_warning(self):
        self.exec_replies = [pitlib.CommandError('x', 255, 'Agent error: The command guest-exec has not been found')]
        meta = self.deep()
        self.assertEqual(len(meta['warnings']), 1)
        self.assertIn('cannot exec', meta['warnings'][0])

    def test_deep_smoke_skips_cloud_init_when_it_is_not_installed(self):
        self.exec_replies = [pitlib.CommandError('x', 255, "Failed to execute child process “cloud-init” (No such file "
                                                           "or directory)"), self.exec_json(0)]
        meta = self.deep()
        self.assertNotIn('warnings', meta)
        self.assertEqual(self.exec_replies, [])

    def test_smoke_full_clone_fallback_retries_on_collision(self):
        self.clone_error = 'Linked clone feature is not supported for drive scsi0'
        tmp_ids = iter([9999, 9998])
        orig = pitlib.run_cmd.side_effect

        def run_cmd(argv, **kw):
            a = [str(x) for x in argv]
            if a[:4] == ['qm', 'clone', '9042', '9999'] and a[-1] == '1':
                self.commands.append(a)
                raise pitlib.CommandError('clone failed', 255, 'unable to create VM 9999: config file already exists')
            return orig(argv, **kw)
        pitlib.run_cmd.side_effect = run_cmd
        self.build(9042, smoke_timeout=60, smoke_allocate=lambda: next(tmp_ids))
        full = [c for c in self.commands if c[:2] == ['qm', 'clone'] and c[-1] == '1']
        self.assertEqual([c[3] for c in full], ['9999', '9998'])

    def test_cleanup_commands_have_timeouts(self):
        imp.destroy_vm_quietly(9042)
        self.assertIn('timeout', imp.subprocess.run.call_args.kwargs)
