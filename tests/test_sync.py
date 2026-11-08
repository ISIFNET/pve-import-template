# -*- coding: utf-8 -*-
"""sync-templates.py 决策 / 切换 / 回收单测：PVE 命令全部打桩（不需要真实节点）。"""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pitlib  # noqa: E402

spec = importlib.util.spec_from_file_location('synctemplates', os.path.join(ROOT, 'sync-templates.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class FakeNode:
    """一台节点：VM 表 + 配置；qm 命令改它，pvesh 读它。"""

    def __init__(self):
        self.vms = {}
        self.configs = {}
        self.commands = []

    def add(self, vmid, name, template=True, tags=(), meta=None, lock='', cfg=None):
        self.vms[vmid] = {'vmid': vmid, 'name': name, 'template': template, 'status': 'stopped',
                          'tags': list(tags), 'lock': lock}
        c = {'scsi0': 'local-lvm:base-%d-disk-0,discard=on' % vmid, 'net0': 'virtio=AA,bridge=vmbr1',
             'memory': '512', 'cores': '1', 'boot': 'order=scsi0'}
        if meta:
            c['description'] = pitlib.encode_description(meta)
        c.update(cfg or {})
        self.configs[vmid] = c

    def node_vms(self, _node):
        return [dict(v) for v in sorted(self.vms.values(), key=lambda v: v['vmid'])]

    def vm_config(self, _node, vmid):
        return dict(self.configs.get(int(vmid), {}))

    def run_cmd(self, argv, timeout=None, env=None, log=None):
        argv = [str(a) for a in argv]
        self.commands.append(argv)
        if argv[:2] == ['qm', 'set'] and '--name' in argv:
            vmid = int(argv[2])
            self.vms[vmid]['name'] = argv[argv.index('--name') + 1]
            if '--tags' in argv:
                self.vms[vmid]['tags'] = pitlib.parse_tags(argv[argv.index('--tags') + 1])
        if argv[:2] == ['qm', 'destroy']:
            self.vms.pop(int(argv[2]), None)
        return ''


class FakeImport:
    TOOL_VERSION = '2.0.0'

    def __init__(self, node, templates, fail=None, retired=None):
        self.node = node
        self.templates = templates
        self.retired = retired or []
        self.fail = fail or {}
        self.built = []
        self.extra_configs = []

    def load_config(self):
        return {'templates': self.templates, 'default_profile': 'standard', 'retired': self.retired,
                'profiles': {'standard': [t['name'] for t in self.templates if not t.get('in_full_only')]}}

    def retired_templates(self, config):
        return [{'name': n} for n in config.get('retired') or []]

    def match_templates(self, tpls, expr):
        if '@' in expr:
            expr = pitlib.expand_profiles(expr, self.load_config())
        sels = expr.split(',')
        return [t for t in tpls if any(pitlib.name_matches(t['name'], s) for s in sels)]

    def recipe_of(self, tpl, tuning, upgrade, mirror):
        return 'r1'

    def source_unchanged(self, meta, head):
        return meta.get('etag') == head.get('etag')

    def disable_tls_verification(self):
        pass

    def upstream_checksum(self, tpl, url, config=None):
        return self.checksums.get(tpl['name'])

    checksums = {}

    def build_template(self, tpl, storage, vmid, *, vm_name, **kw):
        if tpl['name'] in self.fail:
            raise self.fail[tpl['name']]
        vid = vmid() if callable(vmid) else vmid
        self.built.append((tpl['name'], storage, vid, vm_name, kw.get('vm_opts'), kw.get('source_url'), kw.get('smoke_timeout')))
        self.extra_configs.append(kw.get('extra_config'))
        meta = {'v': 1, 'name': tpl['name'], 'built_at': int(time.time()), 'source': {'url': tpl['url']},
                'checksum_verified': True, 'recipe': 'r1', 'tool': '2.0.0', 'vmid': vid,
                'carried': sorted((kw.get('extra_config') or {}).keys())}
        # 新模板的实际配置带上沿用的键（sync 切换前会读回来核对 hookscript）
        cfg = {k: v for k, v in (kw.get('extra_config') or {}).items() if k not in self.drop_keys}
        self.node.add(vid, vm_name, meta=meta, tags=['pit'], cfg=cfg)
        return meta

    drop_keys = ()


def args_for(argv):
    return sync.parse_args(argv)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.node = FakeNode()
        patches = [
            mock.patch.object(pitlib, 'node_vms', self.node.node_vms),
            mock.patch.object(pitlib, 'vm_config', self.node.vm_config),
            mock.patch.object(pitlib, 'run_cmd', self.node.run_cmd),
            mock.patch.object(pitlib, 'cluster_used_vmids', lambda: set(self.node.vms)),
            mock.patch.object(pitlib, 'local_node_name', lambda: 'pve1'),
            mock.patch.object(pitlib, 'linked_clone_refs', lambda vmid, nodes_dir=None: []),
            mock.patch.object(pitlib, 'http_head', lambda url: {'etag': 'e1'}),
            mock.patch.object(sync, 'STATE_FILE', os.path.join(self.tmp.name, 'last-run.json')),
            # 2.2.0：宿主机探测 / 存储查询 / 共享游标全部打桩（不读这台测试机的 /proc、不跑 pvesh）
            mock.patch.object(pitlib, 'SHARED_HW_FILE', os.path.join(self.tmp.name, 'pit-vmid-hw')),
            mock.patch.object(pitlib, 'host_cpu_level', lambda path=None: 4),
            mock.patch.object(pitlib, 'mem_available_mb', lambda path=None: 64 * 1024),
            mock.patch.object(pitlib, 'storage_type', lambda storage: 'lvmthin'),
            mock.patch.object(pitlib, 'storage_config', lambda storage: {'type': 'lvmthin', 'content': 'images,rootdir'}),
            mock.patch.object(pitlib, 'cluster_quorate', lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.conf = sync.load_conf('/nonexistent', explicit=False)

    def run_with(self, imp, argv, conf=None):
        with mock.patch.object(sync, 'load_import_module', lambda: imp):
            return sync.run_sync(args_for(argv), conf or self.conf)

    def fresh_meta(self, name, url, days_old=1, recipe='r1'):
        return {'v': 1, 'name': name, 'built_at': int(time.time() - days_old * 86400),
                'source': {'url': url, 'etag': 'e1'}, 'recipe': recipe, 'tool': '2.0.0'}

    def test_forced_rebuild_swaps_names_without_gap_and_keeps_hardware(self):
        self.node.add(9112, 'alpineLinux-3.22')  # legacy：没有元数据
        imp = FakeImport(self.node, [{'name': 'alpineLinux-3.22', 'url': 'https://a/x.qcow2'}])

        report = self.run_with(imp, ['alpine*', '--force'])

        row = report['templates'][0]
        self.assertEqual(row['action'], 'built')
        self.assertEqual(row['reason'], 'forced')
        new = row['new_vmid']
        self.assertEqual(self.node.vms[new]['name'], 'alpineLinux-3.22')
        self.assertTrue(self.node.vms[9112]['name'].startswith('alpineLinux-3.22-retired-'))
        self.assertIn('pit-retired', self.node.vms[9112]['tags'])
        # 先让新模板接管名字，再退役旧的（中间两个同名都可克隆，没有空窗）
        renames = [c for c in self.node.commands if c[:2] == ['qm', 'set'] and '--name' in c]
        self.assertEqual(int(renames[0][2]), new)
        self.assertEqual(int(renames[1][2]), 9112)
        # 沿用旧模板的存储与网桥
        _name, storage, _vmid, vm_name, vm_opts, source_url, smoke = imp.built[0]
        self.assertEqual(storage, 'local-lvm')
        self.assertEqual(vm_opts['bridge'], 'vmbr1')
        self.assertTrue(vm_name.endswith(sync.BUILD_SUFFIX))
        self.assertEqual(source_url, 'https://a/x.qcow2')   # 决策用的地址直接交给构建
        self.assertEqual(smoke, 600)                         # 默认开机冒烟
        self.assertGreater(new, 9112)                        # 单调分配：不往回复用小号
        # 刚退役的不删（宽限期），keep=1 也保留
        self.assertIn(9112, self.node.vms)
        self.assertTrue(report['ok'])

    def test_current_template_is_left_alone(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13.qcow2'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}])
        report = self.run_with(imp, [])
        self.assertEqual(report['templates'][0]['action'], 'current')
        self.assertEqual(imp.built, [])

    def test_reasons_stale_recipe_and_source(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13.qcow2', days_old=40))
        self.node.add(9011, 'debian-12', meta=self.fresh_meta('debian-12', 'https://d/12.qcow2', recipe='old'))
        self.node.add(9012, 'ubuntu-24.04', meta=self.fresh_meta('ubuntu-24.04', 'https://u/old.img'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'},
                                     {'name': 'debian-12', 'url': 'https://d/12.qcow2'},
                                     {'name': 'ubuntu-24.04', 'url': 'https://u/new.img'}])
        report = self.run_with(imp, ['--max-builds', '0'])
        reasons = {r['name']: r['reason'] for r in report['templates']}
        self.assertEqual(reasons, {'debian-13': 'stale', 'debian-12': 'recipe-changed', 'ubuntu-24.04': 'source-changed'})

    def test_max_builds_defers_by_priority(self):
        self.node.add(9010, 'debian-13')  # legacy
        self.node.add(9012, 'ubuntu-24.04', meta=self.fresh_meta('ubuntu-24.04', 'https://u/old.img'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'},
                                     {'name': 'ubuntu-24.04', 'url': 'https://u/new.img'}])
        report = self.run_with(imp, ['--max-builds', '1'], dict(self.conf, SOURCE_DELAY_HOURS='0'))
        actions = {r['name']: r['action'] for r in report['templates']}
        # source-changed（上游新镜像）优先于 legacy（金丝雀节点：新镜像不等待）
        self.assertEqual(actions, {'ubuntu-24.04': 'built', 'debian-13': 'deferred'})

    def test_absent_templates_are_not_imported_without_add_missing(self):
        imp = FakeImport(self.node, [{'name': 'fedora-45', 'url': 'https://f/45.qcow2'}])
        report = self.run_with(imp, [])
        self.assertEqual(report['templates'][0]['action'], 'absent')
        report = self.run_with(imp, ['fedora-45', '--add-missing'])
        self.assertEqual(report['templates'][0]['action'], 'failed')  # 没配存储
        report = self.run_with(imp, ['fedora-45', '--add-missing', '--storage', 'tank'])
        self.assertEqual(report['templates'][0]['action'], 'built')
        self.assertEqual(imp.built[-1][1], 'tank')

    def test_add_missing_without_selector_uses_the_default_profile_and_never_eol(self):
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d'},
                                     {'name': 'fedora-45', 'url': 'https://f', 'in_full_only': True},
                                     {'name': 'debian-10', 'url': 'https://o', 'eol': True}])
        report = self.run_with(imp, ['--add-missing', '--storage', 'tank', '--max-builds', '0'])
        actions = {r['name']: r['action'] for r in report['templates']}
        self.assertEqual(actions, {'debian-13': 'built', 'fedora-45': 'absent', 'debian-10': 'absent'})

    def test_missing_templates_back_off_after_a_failure(self):
        err = pitlib.PitError('smoke test failed: …')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d'}], fail={'debian-13': err})
        report = self.run_with(imp, ['debian-13', '--add-missing', '--storage', 'tank'])
        self.assertEqual(report['templates'][0]['action'], 'failed')
        sync.save_state(report)
        again = self.run_with(imp, ['debian-13', '--add-missing', '--storage', 'tank'])
        self.assertEqual(again['templates'][0]['action'], 'backoff')

    def test_skipped_templates_do_not_take_build_slots(self):
        self.node.add(9010, 'debian-13', cfg={'bios': 'ovmf'})
        self.node.add(9011, 'debian-12')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-12', 'url': 'https://d/12'}])
        report = self.run_with(imp, ['--max-builds', '1'])
        self.assertEqual({r['name']: r['action'] for r in report['templates']}, {'debian-13': 'skipped', 'debian-12': 'built'})

    def test_adopt_legacy_off_keeps_old_templates(self):
        self.node.add(9010, 'debian-13')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, [], dict(self.conf, ADOPT_LEGACY='0'))
        # 2.2.0 起行 action 收敛到固定词表：旧的 action=legacy 改为 skipped + reason=legacy
        self.assertEqual(report['templates'][0]['action'], 'skipped')
        self.assertEqual(report['templates'][0]['reason'], 'legacy')
        self.assertEqual(imp.built, [])

    def test_eol_templates_sort_last_and_are_not_rebuilt_by_age(self):
        self.node.add(9010, 'debian-10')
        self.node.add(9011, 'debian-13')
        self.node.add(9012, 'ubuntu-18.04', meta=self.fresh_meta('ubuntu-18.04', 'https://u/18', days_old=90))
        imp = FakeImport(self.node, [{'name': 'debian-10', 'url': 'https://d/10', 'eol': True},
                                     {'name': 'debian-13', 'url': 'https://d/13'},
                                     {'name': 'ubuntu-18.04', 'url': 'https://u/18', 'eol': '2023-05-31'}])
        report = self.run_with(imp, ['--max-builds', '1'])
        actions = {r['name']: r['action'] for r in report['templates']}
        self.assertEqual(actions, {'debian-10': 'deferred', 'debian-13': 'built', 'ubuntu-18.04': 'current'})

    def test_new_template_inherits_the_hookscript(self):
        self.node.add(9010, 'debian-13', cfg={'hookscript': 'local:snippets/hook.sh'})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        self.run_with(imp, ['--force'])
        self.assertEqual(imp.extra_configs[-1], {'hookscript': 'local:snippets/hook.sh'})

    def test_failure_keeps_old_template_and_backs_off(self):
        self.node.add(9010, 'debian-13')
        err = sync.pitlib.PitError('virt-customize failed (exit 1): [upgrade] apt-get dist-upgrade failed')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}], fail={'debian-13': err})

        report = self.run_with(imp, [])
        self.assertEqual(report['templates'][0]['action'], 'failed')
        self.assertFalse(report['ok'])
        self.assertEqual(self.node.vms[9010]['name'], 'debian-13')  # 旧模板原样在役
        sync.save_state(report)

        again = self.run_with(imp, [])
        self.assertEqual(again['templates'][0]['action'], 'backoff')
        forced = self.run_with(imp, ['--force'])
        self.assertEqual(forced['templates'][0]['action'], 'failed')

    def test_backoff_windows(self):
        now = 1_000_000
        tpl = {'url': 'https://x/a'}
        fail = {'at': now - 3600, 'recipe': 'r', 'url': sync.source_key(tpl), 'kind': 'build', 'count': 1}
        self.assertGreater(sync.backoff_remaining(fail, tpl, 'r', 86400, now), 0)
        self.assertEqual(sync.backoff_remaining(fail, tpl, 'other', 86400, now), 0)        # 配方变了
        self.assertEqual(sync.backoff_remaining(fail, {'url': 'https://x/b'}, 'r', 86400, now), 0)  # yaml 修了
        self.assertEqual(sync.backoff_remaining(dict(fail, count=3, at=now - 3 * 86400), tpl, 'r', 86400, now),
                         86400)  # 第 3 次失败：4 天窗口，已过 3 天
        gone = dict(fail, kind='not-found', at=now - 5 * 86400)
        self.assertEqual(sync.backoff_remaining(gone, tpl, 'r', 3600, now), 2 * 86400)
        self.assertEqual(sync.backoff_remaining(None, tpl, 'r', 3600, now), 0)

    def test_gc_only_touches_our_retired_templates(self):
        old = time.time() - 30 * 86400
        self.node.add(9030, 'debian-13-retired-old', tags=['pit-retired'])           # 时间戳解析不出
        self.node.add(9031, pitlib.retired_name('debian-13', old))                      # 没有 pit-retired tag
        deleted, _notes = sync.collect_garbage('pve1', 'debian-13', 0, time.time(), dry_run=False)
        self.assertEqual(deleted, [])
        destroys = [c for c in self.node.commands if c[:2] == ['qm', 'destroy']]
        self.assertEqual(destroys, [])

    def test_gc_never_passes_destroy_unreferenced_disks(self):
        self.node.add(9020, pitlib.retired_name('debian-13', time.time() - 30 * 86400), tags=['pit-retired'])
        sync.collect_garbage('pve1', 'debian-13', 0, time.time(), dry_run=False)
        destroy = [c for c in self.node.commands if c[:2] == ['qm', 'destroy']][0]
        self.assertNotIn('--destroy-unreferenced-disks', destroy)

    def test_gc_respects_grace_linked_clones_and_keep(self):
        old = time.time() - 30 * 86400
        self.node.add(9020, pitlib.retired_name('debian-13', old), tags=['pit-retired'])
        self.node.add(9021, pitlib.retired_name('debian-13', old + 86400), tags=['pit-retired'])
        self.node.add(9022, pitlib.retired_name('debian-13', old + 2 * 86400), tags=['pit-retired'],
                      cfg={'protection': '1'})
        self.node.add(9023, pitlib.retired_name('debian-13', time.time() - 60), tags=['pit-retired'])
        with mock.patch.object(pitlib, 'linked_clone_refs', lambda vmid, nodes_dir=None: [555] if vmid == 9021 else []):
            deleted, notes = sync.collect_garbage('pve1', 'debian-13', 0, time.time(), dry_run=False)
        self.assertEqual(deleted, [9020])
        joined = ' '.join(notes)
        self.assertIn('9021: still used by linked clones 555', joined)
        self.assertIn('9022: protection is on', joined)
        self.assertIn('9023: retired less than 12 h ago', joined)

    def test_uefi_template_is_skipped_not_failed(self):
        self.node.add(9010, 'debian-13', cfg={'bios': 'ovmf'})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}])
        report = self.run_with(imp, [])
        self.assertEqual(report['templates'][0]['action'], 'skipped')
        self.assertIn('firmware', report['templates'][0]['detail'])
        self.assertTrue(report['ok'])
        self.assertNotIn('debian-13', report['failures'])

    def test_profile_from_conf_limits_selection(self):
        self.node.add(9010, 'debian-13')
        self.node.add(9011, 'fedora-45')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d'}, {'name': 'fedora-45', 'url': 'https://f'}])
        conf = dict(self.conf, SELECTOR='debian-*')
        report = self.run_with(imp, ['--max-builds', '0', '--dry-run'], conf)
        self.assertEqual([r['name'] for r in report['templates']], ['debian-13'])

    def test_dry_run_changes_nothing(self):
        self.node.add(9010, 'debian-13')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}])
        report = self.run_with(imp, ['--dry-run'])
        self.assertEqual(report['templates'][0]['action'], 'would-build')
        self.assertEqual(imp.built, [])
        self.assertEqual(self.node.commands, [])
        self.assertFalse(os.path.exists(sync.STATE_FILE))

    def test_selector_validation(self):
        with self.assertRaises(SystemExit):
            with mock.patch('sys.stderr'):
                sync.parse_args(['debian;reboot'])

    def test_retire_timestamp_is_taken_at_retire_time(self):
        # 构建花了很久：退役名里的时间必须是改名这一刻，否则 keep=0 时宽限期当场作废
        self.node.add(9112, 'alpineLinux-3.22')
        imp = FakeImport(self.node, [{'name': 'alpineLinux-3.22', 'url': 'https://a/x.qcow2'}])
        before = time.time()
        self.run_with(imp, ['--force', '--keep', '0'])
        self.assertIn(9112, self.node.vms)  # 刚退役，没被同一轮回收
        self.assertGreaterEqual(pitlib.retired_at(self.node.vms[9112]['name']), int(before) // 60 * 60)

    def test_duplicate_actives_are_retired_on_the_next_run(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13.qcow2'))
        older = self.fresh_meta('debian-13', 'https://d/13.qcow2', days_old=2)
        self.node.add(9005, 'debian-13', meta=older)
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}])
        report = self.run_with(imp, [])
        self.assertEqual(report['templates'][0]['action'], 'current')
        self.assertEqual(report['templates'][0]['retired'], [9005])
        self.assertTrue(self.node.vms[9005]['name'].startswith('debian-13-retired-'))
        self.assertEqual(self.node.vms[9010]['name'], 'debian-13')

    def test_templates_outside_the_platform_range_are_left_alone(self):
        self.node.add(900, 'debian-13')  # 运维手工做在区间外的
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}])
        report = self.run_with(imp, ['--force'])
        self.assertEqual(report['templates'][0]['action'], 'absent')
        self.assertEqual(self.node.vms[900]['name'], 'debian-13')

    def test_checksum_drives_republish_detection_not_etag(self):
        meta = self.fresh_meta('fedora-45', 'https://f/45.qcow2')
        meta['source']['checksum'] = 'a' * 64
        self.node.add(9010, 'fedora-45', meta=meta)
        tpl = {'name': 'fedora-45', 'url': 'https://f/45.qcow2', 'checksum': '{url}.sha256'}
        imp = FakeImport(self.node, [tpl])
        imp.checksums = {'fedora-45': 'a' * 64}
        with mock.patch.object(pitlib, 'http_head', lambda url: {'etag': 'different-mirror'}):
            self.assertEqual(self.run_with(imp, ['--dry-run'])['templates'][0]['action'], 'current')
            imp.checksums = {'fedora-45': 'b' * 64}
            self.assertEqual(self.run_with(imp, ['--dry-run'])['templates'][0]['reason'], 'source-changed')

    def test_stale_build_and_smoke_leftovers_are_cleaned_but_not_lookalikes(self):
        meta = {'v': 1, 'name': 'debian-13'}
        self.node.add(9040, 'debian-13-pitbuild', template=True, tags=['pit'], meta=meta)     # 改名前被杀
        self.node.add(9041, 'pitsmoke-9040', template=False, tags=['pit-smoke'])
        self.node.vms[9041]['status'] = 'running'
        self.node.add(9042, 'ubuntu-24.04-pitbuild', template=False, tags=[])                 # 没有 pit tag
        self.node.add(9043, 'rocky-9-pitbuild', template=False, tags=['pit'], meta={'v': 1, 'name': 'other'})
        sync.cleanup_stale_builds('pve1', self.conf, dry_run=False)
        self.assertNotIn(9040, self.node.vms)
        self.assertNotIn(9041, self.node.vms)
        self.assertIn(9042, self.node.vms)
        self.assertIn(9043, self.node.vms)

    def test_selectors_cannot_smuggle_options(self):
        # agent 在选择器前加了 `--`：之后的 --insecure 是选择器，必须被拒；缩写 / 未知选项同样拒
        self.assertEqual(sync.parse_args(['--', 'alpine*']).selectors, ['alpine*'])
        for bad in (['--', '--insecure'], ['--', '-x'], ['--insec'], ['-x']):
            with self.assertRaises(SystemExit):
                with mock.patch('sys.stderr'):
                    sync.parse_args(bad)

    def test_failure_kind(self):
        self.assertEqual(sync.failure_kind(pitlib.PitError('smoke test failed: …')), 'smoke')
        self.assertEqual(sync.failure_kind(pitlib.LaunchError('x')), 'appliance')
        self.assertEqual(sync.failure_kind(pitlib.PitError('insufficient space on storage x')), 'space')
        e = RuntimeError('gone')
        e.kind = 'not-found'
        self.assertEqual(sync.failure_kind(e), 'not-found')


if __name__ == '__main__':
    unittest.main()
