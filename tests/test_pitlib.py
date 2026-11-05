# -*- coding: utf-8 -*-
"""pitlib / import.py 纯逻辑单测（不需要 PVE；python3 -m unittest discover -s tests）。"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pitlib  # noqa: E402


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


imp = load_module('pveimport', 'import.py')


class TagsAndMetaTest(unittest.TestCase):
    def test_tags_roundtrip_dedupes_and_lowercases(self):
        self.assertEqual(pitlib.parse_tags('pit;PIT-b20261105, foo'), ['pit', 'pit-b20261105', 'foo'])
        self.assertEqual(pitlib.format_tags(['pit', 'Pit', 'pit-retired', '']), 'pit;pit-retired')
        self.assertEqual(pitlib.parse_tags(None), [])

    def test_meta_roundtrip_and_garbage(self):
        meta = {'v': 1, 'name': 'debian-13', 'built_at': 1762300000, 'source': {'url': 'https://x/y.qcow2'}, 'tool': '2.0.0'}
        desc = pitlib.encode_description(meta)
        self.assertEqual(pitlib.decode_meta(desc), meta)
        self.assertIsNone(pitlib.decode_meta('hand written notes'))
        self.assertIsNone(pitlib.decode_meta('pit-meta: {not json'))
        self.assertIsNone(pitlib.decode_meta('pit-meta: {"v": 99}'))

    def test_build_tag_is_utc_date(self):
        self.assertEqual(pitlib.build_tag(0), 'pit-b19700101')


class RetiredAndGcTest(unittest.TestCase):
    def test_retired_naming(self):
        ts = time.mktime((2026, 11, 5, 12, 30, 0, 0, 0, 0)) - time.timezone
        name = pitlib.retired_name('alpineLinux-3.22', ts)
        self.assertEqual(name, 'alpineLinux-3.22-retired-202611051230')
        self.assertTrue(pitlib.retired_of(name, 'ALPINElinux-3.22'))
        self.assertFalse(pitlib.retired_of('alpineLinux-3.22', 'alpineLinux-3.22'))
        self.assertTrue(pitlib.is_retired({'name': 'x', 'tags': ['pit-retired']}))

    def test_gc_keeps_newest(self):
        vms = [{'vmid': 9001, 'name': 'd-retired-202601010000', 'tags': ['pit-retired']},
               {'vmid': 9005, 'name': 'd-retired-202611010000', 'tags': ['pit-retired']},
               {'vmid': 9003, 'name': 'd-retired-202606010000', 'tags': ['pit-retired']},
               {'vmid': 9009, 'name': 'd-retired-old', 'tags': ['pit-retired']},
               {'vmid': 9010, 'name': 'd-retired-202601010000', 'tags': []}]
        self.assertEqual([v['vmid'] for v in pitlib.gc_candidates(vms, 1)], [9003, 9001])
        self.assertEqual(pitlib.gc_candidates(vms, 5), [])
        self.assertEqual(len(pitlib.gc_candidates(vms, 0)), 3)

    def test_allocate_vmid_cursor_survives_a_top_of_range_vm(self):
        # 9999 上有个金模板：旧算法 max(in_range)+1 越界、一开局就回绕成「取最小空闲号」
        self.assertEqual(pitlib.allocate_vmid({9001, 9002, 9005, 9999}, 9001, 9999), 9006)
        self.assertEqual(pitlib.allocate_vmid({9001, 9002, 9999}, 9001, 9999, high_water=9998), 9003)  # 游标到顶回绕
        self.assertEqual(pitlib.allocate_vmid_top({9999, 9998}, 9001, 9999), 9997)

    def test_eol_dates(self):
        self.assertTrue(pitlib.is_eol({'eol': True}))
        self.assertFalse(pitlib.is_eol({}))
        self.assertTrue(pitlib.is_eol({'eol': '2026-01-01'}, today='2026-11-05'))
        self.assertFalse(pitlib.is_eol({'eol': '2027-05-31'}, today='2026-11-05'))

    def test_carry_over_only_whitelisted_policy_keys(self):
        cfg = {'hookscript': 'local:snippets/hook.sh', 'vga': 'serial0', 'machine': 'q35', 'args': '-evil',
               'net0': 'virtio,bridge=vmbr0'}
        self.assertEqual(pitlib.carry_over(cfg), {'hookscript': 'local:snippets/hook.sh', 'vga': 'serial0', 'machine': 'q35'})
        self.assertEqual(pitlib.carry_over({'hookscript': 'local:snippets/../../etc/x;rm'}), {})

    def test_disk_format_follows_the_old_disk_or_storage_type(self):
        self.assertEqual(pitlib.disk_format_for('local', 'local:9001/base-9001-disk-0.qcow2'), 'qcow2')
        with mock.patch.object(pitlib, 'storage_type', return_value='dir'):
            self.assertEqual(pitlib.disk_format_for('local'), 'qcow2')
        with mock.patch.object(pitlib, 'storage_type', return_value='lvmthin'):
            self.assertIsNone(pitlib.disk_format_for('local-lvm', 'local-lvm:base-9001-disk-0'))

    def test_allocate_vmid_is_monotonic(self):
        # 不回填 9003 的空洞：回收掉的号会被别的系统复用，只记了 VMID 的服务会装错系统
        self.assertEqual(pitlib.allocate_vmid({9001, 9002, 9004}, 9001, 9999), 9005)
        self.assertEqual(pitlib.allocate_vmid({9001}, 9001, 9999, high_water=9100), 9101)
        self.assertEqual(pitlib.allocate_vmid({10000256, 100}, 9001, 9999), 9001)   # 区间外的号不算
        self.assertEqual(pitlib.allocate_vmid({9998, 9999, 9001}, 9001, 9999), 9002)  # 用到顶才回绕
        with self.assertRaises(pitlib.PitError):
            pitlib.allocate_vmid({9001, 9002}, 9001, 9002)

    def test_linked_clone_refs_ignores_own_config(self):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, 'pve1', 'qemu-server')
            os.makedirs(q)
            for f, body in (('9012.conf', 'scsi0: local-lvm:base-9012-disk-0,size=2G\n'),
                            ('100.conf', 'scsi0: local-lvm:base-9012-disk-0/vm-100-disk-0,size=20G\n'),
                            ('101.conf', 'scsi0: local-lvm:vm-101-disk-0,size=20G\n')):
                with open(os.path.join(q, f), 'w') as fh:
                    fh.write(body)
            self.assertEqual(pitlib.linked_clone_refs(9012, nodes_dir=d), [100])
            self.assertEqual(pitlib.linked_clone_refs(9013, nodes_dir=d), [])


class UpstreamTest(unittest.TestCase):
    ALPINE_INDEX = ''.join('<a href="%s">x</a>' % f for f in (
        'generic_alpine-3.22.5-x86_64-bios-cloudinit-r0.qcow2',
        'alpine-3.22.7-x86_64-bios-cloudinit-r0.qcow2',
        'alpine-3.22.10-x86_64-bios-cloudinit-r0.qcow2.sha512',
        'alpine-3.22.7-x86_64-bios-cloudinit-metal-r0.qcow2',
        'alpine-3.22.6-x86_64-bios-cloudinit-r1.qcow2',
    ))

    def test_discover_numeric_ordering_and_full_match(self):
        pattern = r'(?:generic_)?alpine-3\.22\.(\d+)-x86_64-bios-cloudinit-r(\d+)\.qcow2'
        self.assertEqual(pitlib.discover_latest(self.ALPINE_INDEX, pattern), 'alpine-3.22.7-x86_64-bios-cloudinit-r0.qcow2')
        index = self.ALPINE_INDEX + '<a href="alpine-3.22.10-x86_64-bios-cloudinit-r0.qcow2">'
        self.assertEqual(pitlib.discover_latest(index, pattern), 'alpine-3.22.10-x86_64-bios-cloudinit-r0.qcow2')

    def test_discover_tie_break_is_deterministic(self):
        index = ('<a href="CentOS-Stream-GenericCloud-x86_64-9-20250101.0.x86_64.qcow2">'
                 '<a href="CentOS-Stream-GenericCloud-9-20250101.0.x86_64.qcow2">')
        pattern = r'CentOS-Stream-GenericCloud-(?:x86_64-)?9-(\d{8})\.(\d+)\.x86_64\.qcow2'
        picks = {pitlib.discover_latest(index, pattern) for _ in range(20)}
        self.assertEqual(len(picks), 1)

    def test_discover_never_downgrades_below_the_running_version(self):
        tpl = {'name': 'a', 'url': 'https://h/a-5.qcow2', 'discover': {'index': 'https://h/', 'pattern': r'a-(\d+)\.qcow2'}}
        with mock.patch.object(pitlib, 'http_text', return_value='<a href="a-7.qcow2">'):   # 落后的镜像 / 旧缓存
            self.assertEqual(pitlib.resolve_source(tpl, last_known='https://h/a-8.qcow2'), ('https://h/a-8.qcow2', 'discovered'))
            self.assertEqual(pitlib.resolve_source(tpl, last_known='https://h/a-6.qcow2'), ('https://h/a-7.qcow2', 'discovered'))

    def test_inspect_image_rejects_backing_files_and_odd_formats(self):
        def fake(info):
            return mock.patch.object(pitlib, 'run_cmd', return_value=json.dumps(info))
        with fake({'format': 'qcow2'}):
            self.assertEqual(pitlib.inspect_image('/x'), 'qcow2')
        with fake({'format': 'qcow2', 'backing-filename': '/dev/pve/vm-100-disk-0'}):
            with self.assertRaises(pitlib.PitError):
                pitlib.inspect_image('/x')
        with fake({'format': 'qcow2', 'format-specific': {'data': {'data-file': '/etc/shadow'}}}):
            with self.assertRaises(pitlib.PitError):
                pitlib.inspect_image('/x')
        with fake({'format': 'vmdk'}):
            with self.assertRaises(pitlib.PitError):
                pitlib.inspect_image('/x')

    def test_recipe_hash_ignores_install_location(self):
        a = pitlib.recipe_hash({'x': 1}, [os.path.join(pitlib.SCRIPT_DIR, 'uploads', 'install-qga.sh')], '2')
        with tempfile.TemporaryDirectory() as other:
            os.makedirs(os.path.join(other, 'uploads'))
            shutil.copy(os.path.join(pitlib.SCRIPT_DIR, 'uploads', 'install-qga.sh'), os.path.join(other, 'uploads'))
            with mock.patch.object(pitlib, 'SCRIPT_DIR', other):
                b = pitlib.recipe_hash({'x': 1}, [os.path.join(other, 'uploads', 'install-qga.sh')], '2')
        self.assertEqual(a, b)

    def test_discover_centos_both_namings(self):
        index = ('<a href="CentOS-Stream-GenericCloud-x86_64-10-20260728.1.x86_64.qcow2">'
                 '<a href="CentOS-Stream-GenericCloud-10-20261103.0.x86_64.qcow2">'
                 '<a href="CentOS-Stream-GenericCloud-10-latest.x86_64.qcow2">')
        pattern = r'CentOS-Stream-GenericCloud-(?:x86_64-)?10-(\d{8})\.(\d+)\.x86_64\.qcow2'
        self.assertEqual(pitlib.discover_latest(index, pattern), 'CentOS-Stream-GenericCloud-10-20261103.0.x86_64.qcow2')

    def test_parse_checksum_formats(self):
        h256, h512 = 'a' * 64, 'b' * 128
        self.assertEqual(pitlib.parse_checksum('%s *noble.img\n%s  other.img' % (h256, 'c' * 64), 'noble.img'), ('sha256', h256))
        self.assertEqual(pitlib.parse_checksum('# comment\nSHA256 (rocky.qcow2) = %s\n' % h256, 'rocky.qcow2'), ('sha256', h256))
        self.assertEqual(pitlib.parse_checksum('%s  alpine.qcow2\n' % h512, 'alpine.qcow2'), ('sha512', h512))
        self.assertEqual(pitlib.parse_checksum(h512 + '\n', 'whatever'), ('sha512', h512))
        self.assertIsNone(pitlib.parse_checksum('%s  a.img\n%s  b.img\n' % (h256, h256), 'c.img'))

    def test_checksum_url_placeholders(self):
        url = 'https://h/d/x.qcow2'
        self.assertEqual(pitlib.checksum_url({'checksum': '{url}.sha512'}, url), url + '.sha512')
        self.assertEqual(pitlib.checksum_url({'checksum': '{dir}/SHA256SUMS'}, url), 'https://h/d/SHA256SUMS')
        self.assertIsNone(pitlib.checksum_url({}, url))
        html = '<a href="Fedora-Cloud-45-1.4-x86_64-CHECKSUM">'
        self.assertEqual(pitlib.checksum_url({'checksum_pattern': r'Fedora-Cloud-45-(\d+)\.(\d+)-x86_64-CHECKSUM'}, url, html),
                         'https://h/d/Fedora-Cloud-45-1.4-x86_64-CHECKSUM')

    def test_mirror_candidates(self):
        cfg = {'image_mirrors': [{'from': 'https://a/', 'to': ['https://m1/', 'https://m2/x/']}, {'from': 'https://b/', 'to': 'https://m3/'}]}
        self.assertEqual(pitlib.mirror_candidates('https://a/p/f', cfg), ['https://a/p/f', 'https://m1/p/f', 'https://m2/x/p/f'])
        self.assertEqual(pitlib.mirror_candidates('https://b/f', cfg), ['https://b/f', 'https://m3/f'])
        self.assertEqual(pitlib.mirror_candidates('https://c/f', cfg), ['https://c/f'])
        self.assertEqual(pitlib.mirror_candidates('https://c/f', None), ['https://c/f'])

    def test_resolve_source_falls_back_to_last_known_not_pinned(self):
        tpl = {'name': 'a', 'url': 'https://h/old.qcow2', 'discover': {'index': 'https://h/', 'pattern': r'a-(\d+)\.qcow2'}}
        with mock.patch.object(pitlib, 'http_text', side_effect=OSError('down')):
            self.assertEqual(pitlib.resolve_source(tpl, last_known='https://h/a-7.qcow2'), ('https://h/a-7.qcow2', 'fallback'))
            self.assertEqual(pitlib.resolve_source(tpl), ('https://h/old.qcow2', 'fallback'))
        with mock.patch.object(pitlib, 'http_text', return_value='<a href="a-7.qcow2"><a href="a-12.qcow2">'):
            self.assertEqual(pitlib.resolve_source(tpl), ('https://h/a-12.qcow2', 'discovered'))
        self.assertEqual(pitlib.resolve_source({'name': 'b', 'url': 'https://h/b'}), ('https://h/b', 'pinned'))


class ConfAndSelectionTest(unittest.TestCase):
    SCHEMA = {'A': (r'[0-9]+', '1'), 'B': (r'[a-z]*', '')}

    def test_parse_conf(self):
        conf = pitlib.parse_conf_text('# c\nA=5\nB="xy"\nUNKNOWN=$(rm -rf /)\n', self.SCHEMA)
        self.assertEqual(conf, {'A': '5', 'B': 'xy'})
        self.assertEqual(pitlib.parse_conf_text('', self.SCHEMA), {'A': '1', 'B': ''})
        with self.assertRaises(pitlib.PitError):
            pitlib.parse_conf_text('A=5; reboot\n', self.SCHEMA)
        with self.assertRaises(pitlib.PitError):
            pitlib.parse_conf_text('not a pair\n', self.SCHEMA)

    def test_expand_profiles(self):
        cfg = {'profiles': {'minimal': ['debian-13', 'alpine*'], 'full': '*'}}
        self.assertEqual(pitlib.expand_profiles('@minimal,ubuntu-24.04', cfg), 'debian-13,alpine*,ubuntu-24.04')
        self.assertEqual(pitlib.expand_profiles('@full', cfg), '*')
        with self.assertRaises(pitlib.PitError):
            pitlib.expand_profiles('@nope', cfg)

    def test_shipped_profiles_only_reference_real_templates(self):
        cfg = imp.load_config()
        names = {t['name'] for t in cfg['templates']}
        for profile, items in cfg['profiles'].items():
            for item in ([items] if isinstance(items, str) else items):
                if '*' not in item:
                    self.assertIn(item, names, 'profile %s references %s' % (profile, item))
        # 组里只放还在安全支持期内的版本：变红 = 有版本到了 eol 日期，该把它移出组、换上新版本了（sync 本身已不会再补它）
        for profile in ('minimal', 'standard'):
            eol = [t['name'] for t in imp.match_templates(cfg['templates'], '@' + profile, config=cfg) if pitlib.is_eol(t)]
            self.assertEqual(eol, [], 'profile %s contains end-of-life templates' % profile)

    def test_shipped_eol_values_are_bool_or_dates(self):
        for t in imp.load_config()['templates']:
            value = t.get('eol')
            if value is None or isinstance(value, bool):
                continue
            self.assertRegex(str(value), r'^\d{4}-\d{2}-\d{2}$', t['name'])
            time.strptime(str(value), '%Y-%m-%d')

    def test_default_selection_is_the_default_profile(self):
        cfg = imp.load_config()
        self.assertEqual(imp.default_profile(cfg), 'standard')
        names = imp.default_template_names(cfg)
        self.assertEqual(names, [t['name'] for t in imp.match_templates(cfg['templates'], '@standard', config=cfg)])
        self.assertNotIn('ubuntu-18.04', names)   # 旧版「主流系列」按系列选，会把 EOL 的 18.04 / 20.04 / debian-10 一起导进来
        self.assertIn('rocky-9', names)
        self.assertEqual(imp.default_families(cfg)[:2], ['ubuntu', 'debian'])

    def test_eol_metadata_does_not_change_the_recipe(self):
        # 配方变 = 全机队重建；eol 只是支持期元数据（加日期 / 改日期都不该触发）
        tpl = {'name': 'debian-13', 'url': 'https://d/13.qcow2', 'cloud_init': True}
        base = imp.recipe_of(tpl, True, True, None)
        self.assertEqual(imp.recipe_of({**tpl, 'eol': '2030-06-30'}, True, True, None), base)
        self.assertEqual(imp.recipe_of({**tpl, 'eol': True}, True, True, None), base)
        self.assertNotEqual(imp.recipe_of({**tpl, 'net_tuning': False}, True, True, None), base)

    def test_default_selection_without_profiles_falls_back_to_supported_templates(self):
        cfg = {'templates': [{'name': 'debian-10', 'eol': True}, {'name': 'debian-13', 'eol': '2030-06-30'}, {'name': 'archLinux'}]}
        self.assertEqual(imp.default_template_names(cfg), ['debian-13', 'archLinux'])

    def test_match_templates_case_insensitive(self):
        tpls = [{'name': 'alpineLinux-3.22'}, {'name': 'debian-13'}]
        self.assertEqual(imp.match_templates(tpls, 'ALPINELINUX-3.22'), [tpls[0]])
        self.assertEqual(imp.match_templates(tpls, 'Alpine*'), [tpls[0]])
        self.assertEqual(imp.match_templates(tpls, 'alpine'), [tpls[0]])  # 系列名


class ImportPipelineTest(unittest.TestCase):
    def test_source_unchanged_prefers_etag(self):
        self.assertFalse(imp.source_unchanged({'etag': 'a', 'last_modified': 'L'}, {'etag': 'b', 'last_modified': 'L'}))
        self.assertTrue(imp.source_unchanged({'etag': 'a'}, {'etag': 'a'}))
        self.assertTrue(imp.source_unchanged({'last_modified': 'L'}, {'last_modified': 'L'}))
        self.assertFalse(imp.source_unchanged({}, {}))

    def test_customization_order(self):
        tpl = {'name': 'rocky-9', 'customize': {'run': ['./uploads/install-qga.sh']}}
        args = imp.customization_args(tpl, {'rhel': {'url': 'http://m/centos'}}, True, True, '/tmp/info')
        runs = [args[i + 1] for i, a in enumerate(args) if a == '--run']
        self.assertTrue(runs[0].startswith(tempfile.gettempdir()) or 'mirror_setup_' in runs[0])
        self.assertEqual(os.path.basename(runs[1]), 'upgrade-packages.sh')
        self.assertEqual([os.path.basename(r) for r in runs[-2:]], ['verify-image.sh', 'finalize-image.sh'])
        self.assertIn('--selinux-relabel', args)
        imp.cleanup_temp_files()

    def test_upgrade_and_tuning_switches(self):
        tpl = {'name': 'archLinux', 'upgrade': False, 'net_tuning': False}
        args = imp.customization_args(tpl, None, True, True, '/tmp/info')
        names = [os.path.basename(args[i + 1]) for i, a in enumerate(args) if a == '--run']
        self.assertNotIn('upgrade-packages.sh', names)
        self.assertNotIn('apply-net-tuning.sh', names)
        self.assertNotIn('--selinux-relabel', args)

    def test_recipe_changes_with_script_content_not_url(self):
        tpl = {'name': 'x', 'url': 'https://a/1', 'customize': {'run': ['./uploads/install-qga.sh']}}
        base = imp.recipe_of(tpl, True, True, None)
        self.assertEqual(base, imp.recipe_of(dict(tpl, url='https://a/2'), True, True, None))
        self.assertNotEqual(base, imp.recipe_of(tpl, False, True, None))
        with mock.patch.object(imp, 'RECIPE_VERSION', '99'):
            self.assertNotEqual(base, imp.recipe_of(tpl, True, True, None))
        # 发版（TOOL_VERSION）不改配方：改个文档不该让全机队排队重建
        with mock.patch.object(imp, 'TOOL_VERSION', '9.9.9'):
            self.assertEqual(base, imp.recipe_of(tpl, True, True, None))

    def test_debian_mirror_has_no_double_path(self):
        script = imp.build_mirror_command({'debian': {'url': 'http://mirrors.aliyun.com/debian'}}, 'debian')
        self.assertNotIn('/debian/debian', script)
        self.assertIn('http://mirrors.aliyun.com/debian-security', script)

    def test_fedora_is_not_treated_as_rhel_for_mirrors(self):
        self.assertEqual(imp.detect_os_family('fedora-45'), 'fedora')
        self.assertIsNone(imp.build_mirror_command({'rhel': {'url': 'http://m'}}, 'fedora'))

    def test_template_names_are_vm_safe(self):
        for tpl in imp.load_config()['templates']:
            self.assertRegex(tpl['name'], imp.TEMPLATE_NAME_RE)
            if tpl.get('discover') or tpl.get('checksum') or tpl.get('checksum_pattern'):
                continue
            self.fail('%s has no checksum / discover configured' % tpl['name'])


if __name__ == '__main__':
    unittest.main()
