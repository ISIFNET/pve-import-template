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
        # args 2.2.0 起沿用，但只收不带路径 / 引号 / shell 元字符的值：-fw_cfg file=/etc/shadow 之类原样丢弃
        cfg = {'hookscript': 'local:snippets/hook.sh', 'vga': 'serial0', 'machine': 'q35',
               'args': '-fw_cfg name=opt/x,file=/etc/shadow', 'net0': 'virtio,bridge=vmbr0', 'protection': '1'}
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

    def test_retired_list_is_never_built_or_listed(self):
        cfg = imp.load_config()
        live = {t['name'].lower() for t in cfg['templates']}
        retired = {t['name'].lower() for t in imp.retired_templates(cfg)}
        self.assertTrue(retired)
        self.assertEqual(live & retired, set())
        # --all / @full / 系列名都只在 templates 里挑：EOL 的不会被重新导入
        self.assertEqual({t['name'].lower() for t in imp.match_templates(cfg['templates'], '*', config=cfg)} & retired, set())
        with self.assertRaises(pitlib.PitError):
            imp.validate_config({'templates': [{'name': 'debian-11'}], 'retired': ['Debian-11']})
        with self.assertRaises(pitlib.PitError):
            imp.validate_config({'retired': ['bad name;x']})

    def test_alpine_templates_carry_the_openssh_floor(self):
        # 2026-11-05 OpenSSH 10.6p2：musl + 无 PAM 的 sshd 密码认证可被绕过，Alpine 各分支回移的修复版本
        floors = {t['name']: imp.min_packages(t) for t in imp.load_config()['templates'] if t['name'].lower().startswith('alpine')}
        self.assertEqual(floors, {'alpineLinux-3.22': {'openssh-server': '10.0_p1-r11'},
                                  'alpineLinux-3.23': {'openssh-server': '10.2_p1-r1'},
                                  'alpineLinux-3.24': {'openssh-server': '10.3_p1-r2'}})

    def test_min_packages_validation(self):
        self.assertEqual(imp.min_packages({'name': 'x'}), {})
        self.assertEqual(imp.min_packages({'name': 'x', 'min_packages': {'openssh-server': '1:9.6p1-3'}}), {'openssh-server': '1:9.6p1-3'})
        for bad in ({'openssh-server': '10.0;reboot'}, {'open ssh': '1'}, ['openssh-server'], {'openssh-server': None},
                    {'openssh-server': '1\nMIN_PACKAGE=x 0'}):
            with self.assertRaises(pitlib.PitError, msg=repr(bad)):
                imp.min_packages({'name': 'x', 'min_packages': bad})

    def test_raising_a_security_floor_rebuilds_the_template(self):
        tpl = {'name': 'alpineLinux-3.22', 'url': 'https://d/a.qcow2', 'cloud_init': True}
        base = imp.recipe_of(tpl, True, True, None)
        floor = imp.recipe_of({**tpl, 'min_packages': {'openssh-server': '10.0_p1-r11'}}, True, True, None)
        self.assertNotEqual(floor, base)
        self.assertNotEqual(imp.recipe_of({**tpl, 'min_packages': {'openssh-server': '10.0_p1-r12'}}, True, True, None), floor)

    def test_build_info_lists_the_floors_for_verify(self):
        path = imp.write_build_info('alpineLinux-3.22', 0, {'url': 'u', 'sha256': 's'}, {'openssh-server': '10.0_p1-r11'})
        with open(path) as fh:
            self.assertIn('MIN_PACKAGE=openssh-server 10.0_p1-r11\n', fh.read())

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
        args = imp.customization_args(tpl, {'rocky': {'url': 'http://m/rockylinux'}}, True, True, '/tmp/info')
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


class ClusterAndCursorTest(unittest.TestCase):
    def test_quorum(self):
        def status(entries):
            return mock.patch.object(pitlib, 'pvesh_json', return_value=entries)
        with status([{'type': 'cluster', 'name': 'c', 'quorate': 0}, {'type': 'node', 'name': 'pve1'}]):
            self.assertIs(pitlib.cluster_quorate(), False)
        with status([{'type': 'cluster', 'quorate': 1}]):
            self.assertIs(pitlib.cluster_quorate(), True)
        with status([{'type': 'node', 'name': 'pve1', 'online': 1}]):     # 单机：没有 cluster 条目
            self.assertIsNone(pitlib.cluster_quorate())
        with mock.patch.object(pitlib, 'pvesh_json', side_effect=pitlib.CommandError('x')):
            self.assertIsNone(pitlib.cluster_quorate())

    def test_shared_high_water_roundtrip_and_garbage(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'pit-vmid-hw')
            self.assertEqual(pitlib.read_shared_high_water(path), 0)
            self.assertTrue(pitlib.write_shared_high_water(9123, path))
            self.assertEqual(pitlib.read_shared_high_water(path), 9123)
            self.assertEqual(pitlib.combined_high_water(9100, 9001, 9999, path), 9123)
            self.assertEqual(pitlib.combined_high_water(9200, 9001, 9999, path), 9200)
            # 别的节点配了别的区间：它的游标不算
            pitlib.write_shared_high_water(100500, path)
            self.assertEqual(pitlib.combined_high_water(9100, 9001, 9999, path), 9100)
            with open(path, 'w') as fh:
                fh.write('rm -rf /\n')
            self.assertEqual(pitlib.read_shared_high_water(path), 0)
            self.assertFalse(pitlib.write_shared_high_water(9001, os.path.join(d, 'missing', 'x')))


class GuestExecTest(unittest.TestCase):
    def test_parse_guest_exec(self):
        out = 'warning: something\n{\n   "exitcode" : 2,\n   "exited" : 1,\n   "out-data" : "status: done\\n"\n}\n'
        self.assertEqual(pitlib.parse_guest_exec(out), {'exitcode': 2, 'exited': True, 'out': 'status: done\n', 'err': ''})
        self.assertIsNone(pitlib.parse_guest_exec('Agent error: boom'))
        self.assertIsNone(pitlib.parse_guest_exec('{"pid": 12}'))

    def test_guest_exec_errors_are_classified(self):
        def failing(output):
            return mock.patch.object(pitlib, 'run_cmd', side_effect=pitlib.CommandError('qm failed', 255, output))
        with failing("Agent error: The command guest-exec has been disabled for this instance"):
            with self.assertRaises(pitlib.GuestExecUnsupported):
                pitlib.guest_exec(9999, ['cloud-init', 'status'])
        with failing("Agent error: The command guest-exec has not been found"):
            with self.assertRaises(pitlib.GuestExecUnsupported):
                pitlib.guest_exec(9999, ['true'])
        with failing("Agent error: Guest agent command failed, error was 'Failed to execute child process "
                     "“cloud-init” (No such file or directory)'"):
            with self.assertRaises(pitlib.GuestCommandMissing):
                pitlib.guest_exec(9999, ['cloud-init', 'status'])
        with failing('{"exitcode": 1, "exited": 1, "out-data": "status: error"}'):
            self.assertEqual(pitlib.guest_exec(9999, ['cloud-init', 'status'])['exitcode'], 1)
        with mock.patch.object(pitlib, 'run_cmd', return_value='{"exitcode": 0, "exited": 1}') as rc:
            pitlib.guest_exec(9999, ['sh', '-c', 'echo; rm -rf /'])
        argv = [str(a) for a in rc.call_args.args[0]]
        # 来宾命令作为独立 argv 传给 qm（不经宿主机 shell），放在 -- 之后
        self.assertEqual(argv[:7], ['qm', 'guest', 'exec', '9999', '--timeout', '30', '--'])
        self.assertEqual(argv[7:], ['sh', '-c', 'echo; rm -rf /'])


class HostResourcesTest(unittest.TestCase):
    V2 = 'fpu lm cmov cx8 sse sse2 cx16 lahf_lm popcnt sse4_1 sse4_2 ssse3'
    V3 = V2 + ' avx avx2 bmi1 bmi2 f16c fma abm movbe xsave'
    V4 = V3 + ' avx512f avx512bw avx512cd avx512dq avx512vl'

    def cpuinfo(self, flags):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as fh:
            fh.write('processor\t: 0\nvendor_id\t: GenuineIntel\nflags\t\t: %s\n\nprocessor\t: 1\n' % flags)
        self.addCleanup(os.remove, path)
        return path

    def test_host_cpu_level(self):
        self.assertEqual(pitlib.host_cpu_level(self.cpuinfo('fpu lm sse sse2')), 1)
        self.assertEqual(pitlib.host_cpu_level(self.cpuinfo(self.V2)), 2)
        self.assertEqual(pitlib.host_cpu_level(self.cpuinfo(self.V3)), 3)
        self.assertEqual(pitlib.host_cpu_level(self.cpuinfo(self.V4)), 4)
        self.assertEqual(pitlib.host_cpu_level(self.cpuinfo(self.V3.replace(' avx2', ''))), 2)   # 没 AVX2 = 不到 v3
        self.assertIsNone(pitlib.host_cpu_level('/nonexistent/cpuinfo'))

    def test_cpu_model_ok(self):
        for ok in ('host', 'host,flags=+aes', 'max', 'x86-64-v3', 'x86-64-v4', 'cputype=x86-64-v3,flags=+aes'):
            self.assertTrue(pitlib.cpu_model_ok(ok, 3), ok)
        for bad in ('kvm64', 'x86-64-v2-AES', 'qemu64', 'Haswell', ''):
            self.assertFalse(pitlib.cpu_model_ok(bad, 3), bad)
        self.assertTrue(pitlib.cpu_model_ok('x86-64-v2-AES', 2))

    def test_memory(self):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, 'w') as fh:
            fh.write('MemTotal:       65536000 kB\nMemFree:  100 kB\nMemAvailable:    2097152 kB\n')
        self.addCleanup(os.remove, path)
        self.assertEqual(pitlib.mem_available_mb(path), 2048)
        self.assertIsNone(pitlib.mem_available_mb('/nonexistent'))
        with mock.patch.object(pitlib, 'MEMINFO', path):
            pitlib.ensure_memory(2048)
            pitlib.ensure_memory(0)
            with self.assertRaises(pitlib.Deferred) as ctx:
                pitlib.ensure_memory(4096)
        self.assertTrue(str(ctx.exception).startswith('low memory: 2048 MiB available, need 4096 MiB'))


class SpaceTest(unittest.TestCase):
    def test_ensure_space_reserve_is_a_deferral(self):
        pitlib.ensure_space('x', None, 10)                         # 查不到不拦
        pitlib.ensure_space('x', 30 * pitlib.GIB, 5 * pitlib.GIB, reserve=20 * pitlib.GIB)
        with self.assertRaises(pitlib.SpaceError) as ctx:
            pitlib.ensure_space('storage tank', 24 * pitlib.GIB, 5 * pitlib.GIB, reserve=20 * pitlib.GIB)
        self.assertIsInstance(ctx.exception, pitlib.Deferred)
        self.assertTrue(str(ctx.exception).startswith('not enough space: storage tank has 24.0 GiB free'))

    def test_reserves(self):
        self.assertEqual(pitlib.reserve_bytes(100 * pitlib.GIB, *pitlib.CACHE_RESERVE), 10 * pitlib.GIB)
        self.assertEqual(pitlib.reserve_bytes(500 * pitlib.GIB, *pitlib.CACHE_RESERVE), 50 * pitlib.GIB)
        self.assertEqual(pitlib.reserve_bytes(100 * pitlib.GIB, *pitlib.STORAGE_RESERVE), 20 * pitlib.GIB)
        self.assertEqual(pitlib.reserve_bytes(2000 * pitlib.GIB, *pitlib.STORAGE_RESERVE), 200 * pitlib.GIB)
        with mock.patch.object(pitlib, 'fs_space', return_value=(15 * pitlib.GIB, 100 * pitlib.GIB)):
            pitlib.ensure_fs_space('cache', '/x', 4 * pitlib.GIB)
            with self.assertRaises(pitlib.SpaceError):
                pitlib.ensure_fs_space('cache', '/x', 6 * pitlib.GIB)

    def test_storage_space_and_thin_metadata(self):
        status = {'total': 100 * pitlib.GIB, 'avail': 25 * pitlib.GIB}
        cfg = {'type': 'lvmthin', 'vgname': 'pve', 'thinpool': 'data', 'content': 'images,rootdir'}
        lvs = []

        def run_cmd(argv, **kw):
            lvs.append([str(a) for a in argv])
            return '  %s\n' % meta_pct
        with mock.patch.object(pitlib, 'storage_status', return_value=status), \
                mock.patch.object(pitlib, 'storage_config', return_value=cfg), \
                mock.patch.object(pitlib, 'run_cmd', side_effect=run_cmd):
            meta_pct = '42.10'
            pitlib.ensure_storage_space('pve1', 'local-lvm', 4 * pitlib.GIB)
            self.assertEqual(lvs[-1], ['lvs', '--noheadings', '-o', 'metadata_percent', 'pve/data'])
            with self.assertRaises(pitlib.SpaceError):          # 25 - 6 < 20 GiB 保留
                pitlib.ensure_storage_space('pve1', 'local-lvm', 6 * pitlib.GIB)
            meta_pct = '80.00'
            with self.assertRaises(pitlib.SpaceError) as ctx:
                pitlib.ensure_storage_space('pve1', 'local-lvm', 0)
            self.assertIn('thin pool pve/data metadata is 80.0% full', str(ctx.exception))

    def test_storage_template_problem(self):
        def cfg(c):
            return mock.patch.object(pitlib, 'storage_config', return_value=c)
        with cfg({'type': 'lvm', 'content': 'images'}):
            self.assertIn('type lvm', pitlib.storage_template_problem('vg0'))
        with cfg({'type': 'iscsidirect', 'content': 'images'}):
            self.assertIn('cannot hold VM templates', pitlib.storage_template_problem('san'))
        with cfg({'type': 'dir', 'content': 'iso,backup'}):
            self.assertIn('does not allow VM images', pitlib.storage_template_problem('local'))
        with cfg({'type': 'lvmthin', 'content': 'images,rootdir'}):
            self.assertIsNone(pitlib.storage_template_problem('local-lvm'))
        with cfg(None):
            self.assertIsNone(pitlib.storage_template_problem('unknown'))


class LeftoverVolumeTest(unittest.TestCase):
    LIST = ('Volid                         Format  Type      Size VMID\n'
            'local-lvm:vm-9050-disk-0      raw     images    1024 9050\n'
            'local-lvm:base-9042-disk-0/vm-9050-disk-1 raw images 1024 9050\n'
            'local-lvm:vm-90500-disk-0     raw     images    1024 90500\n')

    def test_storage_volumes_and_own_volume(self):
        with mock.patch.object(pitlib, 'run_cmd', return_value=self.LIST):
            vols = pitlib.storage_volumes('local-lvm', 9050)
        self.assertIn('local-lvm:vm-9050-disk-0', vols)
        self.assertTrue(pitlib.own_volume('local-lvm:vm-9050-disk-0', 9050))
        self.assertTrue(pitlib.own_volume('local-lvm:base-9042-disk-0/vm-9050-disk-1', 9050))
        self.assertTrue(pitlib.own_volume('local:9050/vm-9050-cloudinit.qcow2', 9050))
        self.assertFalse(pitlib.own_volume('local-lvm:vm-90500-disk-0', 9050))
        self.assertFalse(pitlib.own_volume('local-lvm:base-9042-disk-0', 9050))
        with mock.patch.object(pitlib, 'run_cmd', side_effect=pitlib.CommandError('storage down')):
            self.assertIsNone(pitlib.storage_volumes('local-lvm', 9050))

    def test_free_leftover_volumes_only_frees_new_own_volumes(self):
        calls = []

        def run_cmd(argv, **kw):
            argv = [str(a) for a in argv]
            calls.append(argv)
            return self.LIST if argv[:2] == ['pvesm', 'list'] else ''
        with mock.patch.object(pitlib, 'run_cmd', side_effect=run_cmd):
            freed = pitlib.free_leftover_volumes('local-lvm', 9050, {'local-lvm:vm-9050-disk-0'})
            self.assertEqual(freed, ['local-lvm:base-9042-disk-0/vm-9050-disk-1'])
            self.assertEqual([c for c in calls if c[:2] == ['pvesm', 'free']],
                             [['pvesm', 'free', 'local-lvm:base-9042-disk-0/vm-9050-disk-1']])
            calls.clear()
            # 建之前没查到（None）= 不知道哪些是别人的：一个都不删
            self.assertEqual(pitlib.free_leftover_volumes('local-lvm', 9050, None), [])
            self.assertEqual(calls, [])


class CarryOverV22Test(unittest.TestCase):
    def test_new_policy_keys(self):
        cfg = {'nameserver': '1.1.1.1 2606:4700::1111', 'searchdomain': 'example.com corp.local',
               'cicustom': 'user=local:snippets/user.yaml,network=local:snippets/net.yaml', 'ciupgrade': '0',
               'numa': '1', 'tablet': '0', 'rng0': 'source=/dev/urandom,max_bytes=1024,period=1000',
               'scsihw': 'virtio-scsi-pci', 'ostype': 'l26', 'args': '-cpu host,+kvm_pv_unhalt',
               'protection': '1', 'onboot': '1'}
        out = pitlib.carry_over(cfg)
        self.assertEqual(set(out), {'nameserver', 'searchdomain', 'cicustom', 'ciupgrade', 'numa', 'tablet', 'rng0',
                                    'scsihw', 'ostype', 'args'})
        for key, bad in (('nameserver', '1.1.1.1;reboot'), ('cicustom', 'user=local:snippets/a.yaml,x=/etc/shadow'),
                         ('rng0', 'source=/etc/shadow'), ('scsihw', 'evil'), ('args', "-drive file='/dev/sda'"),
                         ('args', 'no-leading-dash')):
            self.assertNotIn(key, pitlib.carry_over({key: bad}), bad)

    def test_uncarried_keys_ignore_disks_nics_and_metadata(self):
        old = {'scsi0': 'x', 'ide2': 'x', 'net0': 'x', 'net1': 'x', 'ipconfig0': 'ip=dhcp', 'efidisk0': 'x', 'unused0': 'x',
               'name': 'n', 'description': 'd', 'tags': 't', 'digest': 'x', 'smbios1': 'uuid=1', 'vmgenid': 'x',
               'memory': '512', 'cores': '1', 'cpu': 'host', 'protection': '1', 'hookscript': 'local:snippets/h.sh',
               'sockets': '2', 'sshkeys': 'x', 'hostpci0': '01:00.0', 'args': '-fw_cfg file=/x'}
        new = {'scsi0': 'y', 'hookscript': 'local:snippets/h.sh'}
        self.assertEqual(pitlib.uncarried_keys(old, new), ['args', 'hostpci0', 'sockets', 'sshkeys'])


class VirtCustomizeEnvTest(unittest.TestCase):
    def test_libguestfs_memsize(self):
        with mock.patch.object(pitlib, 'run_cmd', return_value='') as rc, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LIBGUESTFS_MEMSIZE', None)
            pitlib.run_virt_customize('/x.img', ['--run', '/s.sh'])
            self.assertEqual(rc.call_args.kwargs['env']['LIBGUESTFS_MEMSIZE'], '1024')
            os.environ['LIBGUESTFS_MEMSIZE'] = '2048'      # 运维显式设了：不覆盖（run_cmd 会继承环境）
            pitlib.run_virt_customize('/x.img', ['--run', '/s.sh'])
            self.assertNotIn('LIBGUESTFS_MEMSIZE', rc.call_args.kwargs['env'])


class RecipeAndYamlV22Test(unittest.TestCase):
    def test_metadata_keys_do_not_change_the_recipe(self):
        # min_cpu_level / display_name 跟 eol 一样是元数据：加上它们不能触发全机队重建
        tpl = {'name': 'rocky-10', 'url': 'https://r/10.qcow2', 'cloud_init': True}
        base = imp.recipe_of(tpl, True, True, None)
        self.assertEqual(imp.recipe_of({**tpl, 'min_cpu_level': 3}, True, True, None), base)
        self.assertEqual(imp.recipe_of({**tpl, 'display_name': 'Rocky Linux 10'}, True, True, None), base)

    def test_el10_templates_require_x86_64_v3(self):
        cfg = imp.load_config()
        levels = {t['name']: t.get('min_cpu_level') for t in cfg['templates']}
        for name in ('rocky-10', 'almaLinux-10', 'centos-stream-10'):
            self.assertEqual(levels[name], 3, name)
        self.assertIsNone(levels['rocky-9'])


class MirrorScriptTest(unittest.TestCase):
    """生成的换源脚本对着真实格式的 repo 文件跑一遍（只把 /etc/yum.repos.d 换成临时目录）。"""

    ROCKY = ('[baseos]\nname=Rocky Linux $releasever - BaseOS\n'
             'mirrorlist=https://mirrors.rockylinux.org/mirrorlist?arch=$basearch&repo=BaseOS-$releasever$rltype\n'
             '#baseurl=http://dl.rockylinux.org/$contentdir/$releasever/BaseOS/$basearch/os/\ngpgcheck=1\nenabled=1\n')
    ALMA = ('[baseos]\nname=AlmaLinux $releasever - BaseOS\n'
            'mirrorlist=https://mirrors.almalinux.org/mirrorlist/$releasever/baseos\n'
            '# baseurl=https://repo.almalinux.org/almalinux/$releasever/BaseOS/$basearch/os/\nenabled=1\n')
    STREAM = ('[baseos]\nname=CentOS Stream $releasever - BaseOS\n'
              'metalink=https://mirrors.centos.org/metalink?repo=centos-baseos-$stream&arch=$basearch&protocol=https,http\n'
              'enabled=1\n\n[baseos-debug]\n'
              'metalink=https://mirrors.centos.org/metalink?repo=centos-baseos-debug-$stream&arch=$basearch&protocol=https,http\n'
              'enabled=0\n\n[crb]\n'
              'metalink=https://mirrors.centos.org/metalink?repo=centos-crb-$stream&arch=$basearch&protocol=https,http\n'
              'enabled=0\n\n[extras-common]\n'
              'metalink=https://mirrors.centos.org/metalink?repo=centos-extras-sig-extras-common-$stream&arch=$basearch\n')

    def run_script(self, family, url, repo_text):
        script = imp.build_mirror_command({family: {'url': url}}, family)
        with tempfile.TemporaryDirectory() as d:
            repo = os.path.join(d, 'x.repo')
            with open(repo, 'w') as fh:
                fh.write(repo_text)
            path = os.path.join(d, 'mirror.sh')
            with open(path, 'w') as fh:
                fh.write(script.replace('/etc/yum.repos.d', d))
            import subprocess
            subprocess.run(['sh', path], check=True)
            with open(repo) as fh:
                return fh.read()

    def test_rocky_uses_its_own_mirror_path(self):
        out = self.run_script('rocky', 'https://mirrors.example/rockylinux', self.ROCKY)
        self.assertIn('\nbaseurl=https://mirrors.example/rockylinux/$releasever/BaseOS/$basearch/os/\n', out)
        self.assertIn('#mirrorlist=', out)
        self.assertNotIn('centos', out)

    def test_alma_uses_its_own_mirror_path(self):
        out = self.run_script('almalinux', 'https://mirrors.example/almalinux', self.ALMA)
        self.assertIn('\nbaseurl=https://mirrors.example/almalinux/$releasever/BaseOS/$basearch/os/\n', out)
        self.assertIn('#mirrorlist=', out)

    def test_centos_stream_metalink_becomes_baseurl(self):
        out = self.run_script('centos-stream', 'https://mirrors.example/centos-stream', self.STREAM)
        self.assertIn('\nbaseurl=https://mirrors.example/centos-stream/$stream/BaseOS/$basearch/os/\n', out)
        self.assertIn('\nbaseurl=https://mirrors.example/centos-stream/$stream/CRB/$basearch/os/\n', out)
        self.assertIn('\nbaseurl=https://mirrors.example/centos-stream/SIGs/$stream/extras/$basearch/extras-common/\n', out)
        self.assertIn('repo=centos-baseos-debug-$stream', out)    # 认不出的（debug）原样保留

    def test_rocky_and_alma_never_fall_back_to_the_centos_path(self):
        legacy = {'rhel': {'url': 'http://m/centos'}}
        with mock.patch('sys.stderr'):
            self.assertEqual(imp.build_mirror_args(legacy, 'rocky-9'), [])
            self.assertEqual(imp.build_mirror_args(legacy, 'almaLinux-9'), [])
            self.assertEqual(imp.build_mirror_args(legacy, 'centos-stream-10'), [])
        self.assertEqual(imp.detect_rhel_variant('centos-stream-9'), 'centos-stream')
        args = imp.build_mirror_args({'rocky': {'url': 'http://m/rocky'}}, 'rocky-9')
        with open(args[1]) as fh:
            self.assertIn('baseurl=http://m/rocky', fh.read())
        imp.cleanup_temp_files()

    def test_shipped_mirrors_have_per_distro_el_entries(self):
        mirrors = imp.load_config()['mirrors']
        for name in ('aliyun', 'tsinghua', 'tencent', 'ustc'):
            for key in ('rocky', 'almalinux', 'centos-stream'):
                self.assertIn(key, mirrors[name], '%s lacks %s' % (name, key))
                self.assertNotRegex(mirrors[name][key]['url'], r'/centos$')


class FetchImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(imp, 'CACHE_DIR', self.tmp.name),
                  mock.patch.object(pitlib, 'http_head', return_value={'length': '100'}),
                  mock.patch.object(pitlib, 'fs_space', return_value=(500 * pitlib.GIB, 1000 * pitlib.GIB))):
            p.start()
            self.addCleanup(p.stop)

    def test_interrupted_download_leaves_no_partial_file(self):
        def download_any(name, urls, dest):
            with open(dest, 'wb') as fh:
                fh.write(b'half')
            raise KeyboardInterrupt
        with mock.patch.object(imp, 'download_any', side_effect=download_any):
            with self.assertRaises(KeyboardInterrupt):
                imp.fetch_image({'name': 'debian-13', 'url': 'https://d/13'}, url='https://d/13', config={})
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, 'debian-13.img.download')))

    def test_space_shortage_is_a_deferral_not_a_download_failure(self):
        with mock.patch.object(pitlib, 'fs_space', return_value=(11 * pitlib.GIB, 100 * pitlib.GIB)), \
                mock.patch.object(pitlib, 'http_head', return_value={'length': str(2 * pitlib.GIB)}):
            with self.assertRaises(pitlib.SpaceError):
                imp.fetch_image({'name': 'debian-13', 'url': 'https://d/13'}, url='https://d/13', config={})
