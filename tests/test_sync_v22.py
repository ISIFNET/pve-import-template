# -*- coding: utf-8 -*-
"""sync-templates.py 2.2.0 新行为：pending forced、回滚 / hold / release、暂停、金丝雀延迟、资源推迟、quorum、
中断留痕、半成品清理、dry-run / --list 输出、全名字 GC。PVE 命令全部打桩（复用 test_sync 的假节点）。"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_sync  # noqa: E402
from test_sync import FakeImport, pitlib, sync  # noqa: E402


class Base(unittest.TestCase):
    setUp = test_sync.SyncTest.setUp
    run_with = test_sync.SyncTest.run_with
    fresh_meta = test_sync.SyncTest.fresh_meta

    def conf_with(self, **kw):
        return dict(self.conf, **{k: str(v) for k, v in kw.items()})

    def state(self):
        with open(sync.STATE_FILE) as fh:
            return json.load(fh)

    def retired_tpl(self, vmid, name, days_ago, meta=None, lock=''):
        self.node.add(vmid, pitlib.retired_name(name, time.time() - days_ago * 86400), tags=['pit', 'pit-retired'],
                      meta=meta, lock=lock)


class PendingForcedTest(Base):
    def test_forced_rebuilds_deferred_by_max_builds_are_built_later(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        self.node.add(9011, 'debian-12', meta=self.fresh_meta('debian-12', 'https://d/12'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-12', 'url': 'https://d/12'}])
        report = self.run_with(imp, ['--force', '--max-builds', '1'])
        actions = {r['name']: r['action'] for r in report['templates']}
        self.assertEqual(sorted(actions.values()), ['built', 'deferred'])
        waiting = [n for n, a in actions.items() if a == 'deferred'][0]
        self.assertEqual(set(report['pending_forced']), {waiting})
        sync.save_state(report)

        again = self.run_with(imp, [])          # 不带 --force：上次欠下的强制重建照样建
        row = {r['name']: r for r in again['templates']}[waiting]
        self.assertEqual((row['action'], row['reason']), ('built', 'forced'))
        self.assertTrue(row['detail'].startswith('pending forced rebuild since '))
        self.assertEqual(again['pending_forced'], {})

    def test_pending_forced_respects_backoff_unless_forced_again(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        tpl = {'name': 'debian-13', 'url': 'https://d/13'}
        sync.save_state({'pending_forced': {'debian-13': {'at': int(time.time()) - 3600}},
                         'failures': {'debian-13': {'at': int(time.time()) - 60, 'recipe': 'r1', 'url': sync.source_key(tpl),
                                                    'kind': 'build', 'count': 1, 'error': 'boom'}}})
        imp = FakeImport(self.node, [tpl])
        self.assertEqual(self.run_with(imp, [])['templates'][0]['action'], 'backoff')
        self.assertEqual(self.run_with(imp, ['--force'])['templates'][0]['action'], 'built')

    def test_low_memory_defers_without_failure_and_keeps_the_forced_request(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        with mock.patch.object(pitlib, 'mem_available_mb', lambda path=None: 1000):
            report = self.run_with(imp, ['--force'])
        row = report['templates'][0]
        self.assertEqual(row['action'], 'deferred')
        self.assertTrue(row['detail'].startswith('low memory: 1000 MiB available'))
        self.assertEqual(report['failures'], {})
        self.assertIn('debian-13', report['pending_forced'])
        self.assertTrue(report['ok'])
        self.assertEqual(imp.built, [])

    def test_space_shortage_during_build_is_deferred_and_frees_the_slot(self):
        self.node.add(9010, 'debian-13')
        self.node.add(9011, 'debian-12')
        err = pitlib.SpaceError('not enough space: storage local-lvm has 21.0 GiB free, needs 2.5 GiB + 20.0 GiB reserve')
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-12', 'url': 'https://d/12'}],
                         fail={'debian-13': err})
        report = self.run_with(imp, ['--max-builds', '1'])
        rows = {r['name']: r for r in report['templates']}
        self.assertEqual(rows['debian-13']['action'], 'deferred')
        self.assertTrue(rows['debian-13']['detail'].startswith('not enough space:'))
        self.assertEqual(rows['debian-12']['action'], 'built')     # 推迟的不占名额
        self.assertEqual(report['failures'], {})


class RollbackTest(Base):
    BAD = {'url': 'https://a/alpine-3.22.7.qcow2', 'sha256': 'f' * 64, 'checksum': 'c' * 128}

    def setUp(self):
        super().setUp()
        self.tpl = {'name': 'alpineLinux-3.22', 'url': 'https://a/alpine-3.22.7.qcow2', 'checksum': '{url}.sha512'}
        self.imp = FakeImport(self.node, [self.tpl])
        meta = self.fresh_meta('alpineLinux-3.22', self.BAD['url'])
        meta['source'] = dict(self.BAD)
        self.node.add(9050, 'alpineLinux-3.22', tags=['pit', 'pit-b20261106'], meta=meta)
        good = self.fresh_meta('alpineLinux-3.22', 'https://a/alpine-3.22.6.qcow2', days_old=20)
        self.retired_tpl(9040, 'alpineLinux-3.22', 1, meta=good)
        self.retired_tpl(9030, 'alpineLinux-3.22', 20)

    def rollback(self, name='alpinelinux-3.22'):
        with mock.patch.object(sync, 'load_import_module', lambda: self.imp):
            return sync.rollback(sync.parse_args(['--rollback', name]), self.conf)

    def test_rollback_restores_the_newest_retired_version_and_holds(self):
        doc = self.rollback()
        self.assertEqual(self.node.vms[9040]['name'], 'alpineLinux-3.22')          # yaml 原名
        self.assertEqual(self.node.vms[9040]['tags'], ['pit'])                      # 去掉 pit-retired，其它 tag 保留
        self.assertTrue(pitlib.retired_of(self.node.vms[9050]['name'], 'alpineLinux-3.22'))
        self.assertIn('pit-retired', self.node.vms[9050]['tags'])
        hold = doc['holds']['alpineLinux-3.22']
        self.assertEqual({k: hold[k] for k in ('by', 'bad_vmid', 'bad_source', 'restored_vmid')},
                         {'by': 'rollback', 'bad_vmid': 9050, 'bad_source': 'f' * 64, 'restored_vmid': 9040})
        self.assertEqual(hold['bad_url'], self.BAD['url'])
        row = doc['templates'][-1]
        self.assertEqual((row['action'], row['restored_vmid'], row['retired']), ('rolled-back', 9040, [9050]))
        self.assertEqual(self.state()['holds'], doc['holds'])
        # 先恢复、再退役：没有空窗
        renames = [int(c[2]) for c in self.node.commands if c[:2] == ['qm', 'set'] and '--name' in c]
        self.assertEqual(renames, [9040, 9050])

    def test_rollback_skips_locked_versions_and_fails_cleanly_without_any(self):
        self.node.vms[9040]['lock'] = 'backup'
        self.rollback()
        self.assertEqual(self.node.vms[9030]['name'], 'alpineLinux-3.22')
        self.node.vms.clear()
        self.node.add(9050, 'alpineLinux-3.22', meta=self.fresh_meta('alpineLinux-3.22', 'x'))
        with self.assertRaises(sync.RollbackError) as ctx:
            self.rollback()
        self.assertIn('no retired version of alpineLinux-3.22', str(ctx.exception))
        with self.assertRaises(pitlib.PitError):
            self.rollback('not-a-template')

    def test_rollback_is_undone_when_the_bad_template_cannot_be_retired(self):
        orig = self.node.run_cmd

        def run_cmd(argv, **kw):
            if [str(a) for a in argv[:3]] == ['qm', 'set', '9050']:
                raise pitlib.CommandError("can't lock file", 255, 'got timeout')
            return orig(argv, **kw)
        with mock.patch.object(pitlib, 'run_cmd', run_cmd), mock.patch.object(sync, 'RETIRE_ATTEMPTS', 1):
            with self.assertRaises(sync.RollbackError) as ctx:
                self.rollback()
        self.assertIn('rollback undone', str(ctx.exception))
        self.assertTrue(pitlib.retired_of(self.node.vms[9040]['name'], 'alpineLinux-3.22'))
        self.assertIn('pit-retired', self.node.vms[9040]['tags'])
        self.assertEqual(self.load_holds(), {})

    def load_holds(self):
        return sync.state_dict(sync.load_state(), 'holds')

    def test_hold_blocks_every_automatic_reason_and_force(self):
        self.rollback()
        # 恢复的是 20 天前的构建、配方旧：本来会 stale / recipe-changed / source-changed
        with mock.patch.object(FakeImport, 'recipe_of', lambda *a: 'r2'):
            report = self.run_with(self.imp, ['--max-age-days', '1'], self.conf_with(SOURCE_DELAY_HOURS=0))
            row = report['templates'][0]
            self.assertEqual(row['action'], 'held')
            self.assertIn('or --release', row['detail'])
            forced = self.run_with(self.imp, ['--force'])
        self.assertEqual(forced['templates'][0]['action'], 'held')
        self.assertIn('run --release first', forced['templates'][0]['detail'])
        self.assertTrue(forced['ok'])
        self.assertEqual(self.imp.built, [])

    def test_hold_is_released_by_a_new_upstream_image(self):
        self.rollback()
        newer = dict(self.tpl, url='https://a/alpine-3.22.8.qcow2')
        imp = FakeImport(self.node, [newer])
        report = self.run_with(imp, [], self.conf_with(SOURCE_DELAY_HOURS=0))
        row = report['templates'][0]
        self.assertEqual(row['action'], 'built')
        self.assertTrue(row['detail'].startswith('hold released: new upstream image'))
        self.assertEqual(report['holds'], {})

    def test_same_file_republished_with_a_new_checksum_releases_the_hold(self):
        self.rollback()
        self.imp.checksums = {'alpineLinux-3.22': 'c' * 128}                      # 还是那个坏文件
        self.assertEqual(self.run_with(self.imp, [])['templates'][0]['action'], 'held')
        self.imp.checksums = {'alpineLinux-3.22': 'd' * 128}                      # 同名重新发布
        report = self.run_with(self.imp, [], self.conf_with(SOURCE_DELAY_HOURS=0))
        self.assertNotEqual(report['templates'][0]['action'], 'held')
        self.assertEqual(report['holds'], {})

    def test_unreachable_index_never_releases_a_hold(self):
        hold = {'at': 1, 'by': 'rollback', 'bad_url': 'https://a/alpine-3.22.7.qcow2'}
        self.assertFalse(sync.hold_released(hold, self.tpl, 'https://a/alpine-3.22.6.qcow2', 'fallback', lambda t, u: None))
        self.assertTrue(sync.hold_released(hold, self.tpl, 'https://a/alpine-3.22.8.qcow2', 'discovered', lambda t, u: None))
        self.assertFalse(sync.hold_released({'at': 1}, self.tpl, 'https://a/x', 'pinned', lambda t, u: None))

    def test_release_removes_the_hold(self):
        self.rollback()
        doc = sync.release(sync.parse_args(['--release', 'ALPINELINUX-3.22']), self.conf)
        self.assertEqual(doc['holds'], {})
        self.assertEqual(self.load_holds(), {})
        self.assertEqual(sync.release(sync.parse_args(['--release', 'debian-13']), self.conf)['message'], 'no hold on debian-13')


class MainExitCodesTest(Base):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(sync.shutil, 'which', lambda tool: '/usr/bin/' + tool),
                  mock.patch.object(pitlib, 'LOCK_FILE', os.path.join(self.tmp.name, 'lock')),
                  mock.patch.object(pitlib, 'LOG_DIR', os.path.join(self.tmp.name, 'log'))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.close_log)
        self.imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])

    def close_log(self):
        if sync._LOG_FH is not None:
            sync._LOG_FH.close()
            sync._LOG_FH = None

    def main(self, argv, conf_text=''):
        path = os.path.join(self.tmp.name, 'pit.conf')
        with open(path, 'w') as fh:
            fh.write(conf_text)
        out = io.StringIO()
        with mock.patch.object(sync, 'load_import_module', lambda: self.imp), mock.patch('sys.stdout', out):
            code = sync.main(['--config', path, '--json'] + argv)
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None)

    def previous(self):
        return {'failures': {'x': {'at': 1}}, 'holds': {'y': {'at': 2}}, 'pending_forced': {'z': {'at': 3}},
                'sources_seen': {'w': {'key': 'k', 'first_seen': 4}}, 'vmid_high_water': 9123, 'ok': False}

    def test_paused_does_nothing_and_keeps_the_bookkeeping(self):
        self.node.add(9010, 'debian-13')
        sync.save_state(self.previous())
        code, doc = self.main([], 'PAUSED=1\n')
        self.assertEqual(code, 0)
        state = self.state()
        self.assertEqual((state['paused'], state['ok'], state['running'], state['message']),
                         (True, True, False, 'paused (PAUSED=1)'))
        for key, value in self.previous().items():
            if key != 'ok':
                self.assertEqual(state[key], value, key)
        self.assertEqual(self.node.commands, [])
        self.assertEqual(self.imp.built, [])
        self.assertTrue(doc['paused'])

    def test_no_quorum_exits_4_without_recording_failures(self):
        self.node.add(9010, 'debian-13')
        sync.save_state(self.previous())
        with mock.patch.object(pitlib, 'cluster_quorate', lambda: False), mock.patch('sys.stderr'):
            code, _ = self.main([])
        self.assertEqual(code, 4)
        state = self.state()
        self.assertFalse(state['ok'])
        self.assertIn('no quorum', state['message'])
        self.assertEqual(state['failures'], self.previous()['failures'])
        self.assertEqual(self.imp.built, [])

    def test_rollback_and_release_exit_codes(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        with mock.patch('sys.stderr'):
            code, _ = self.main(['--rollback', 'debian-13'])
        self.assertEqual(code, 1)                                              # 没有可恢复的退役版本
        self.retired_tpl(9005, 'debian-13', 2)
        code, doc = self.main(['--rollback', 'debian-13'])
        self.assertEqual(code, 0)
        self.assertIn('debian-13', doc['holds'])
        code, doc = self.main(['--release', 'debian-13'])
        self.assertEqual((code, doc['holds']), (0, {}))

    def test_interrupt_keeps_row_details_and_backs_off_two_hours(self):
        self.node.add(9010, 'debian-13')
        self.node.add(9011, 'debian-12')
        self.imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-12', 'url': 'https://d/12'}],
                              fail={'debian-12': KeyboardInterrupt()})
        with self.assertRaises(KeyboardInterrupt):
            self.main(['--force'])
        state = self.state()
        rows = {r['name']: r for r in state['templates']}
        self.assertEqual(rows['debian-13']['action'], 'built')               # 中断前建好的那个也在明细里
        self.assertEqual((rows['debian-12']['action'], rows['debian-12']['detail']), ('failed', 'interrupted'))
        self.assertEqual(state['failures']['debian-12']['kind'], 'interrupted')
        self.assertIn('debian-12', state['pending_forced'])
        self.assertEqual(state['message'], 'interrupted')
        self.assertEqual(state['summary']['failed'], 1)
        wait = sync.backoff_remaining(state['failures']['debian-12'], {'name': 'debian-12', 'url': 'https://d/12'}, 'r1',
                                      20 * 3600, time.time())
        self.assertTrue(0 < wait <= 2 * 3600)


class CanaryDelayTest(Base):
    def setUp(self):
        super().setUp()
        self.node.add(9012, 'ubuntu-24.04', meta=self.fresh_meta('ubuntu-24.04', 'https://u/old.img'))
        self.imp = FakeImport(self.node, [{'name': 'ubuntu-24.04', 'url': 'https://u/new.img'}])

    def test_new_upstream_waits_then_is_adopted(self):
        report = self.run_with(self.imp, [])
        row = report['templates'][0]
        self.assertEqual((row['action'], row['reason']), ('waiting', 'source-changed'))
        self.assertIn('first seen 0m ago, adopting in 1d0h (SOURCE_DELAY_HOURS=24)', row['detail'])
        self.assertEqual(report['sources_seen']['ubuntu-24.04']['key'], 'new.img')
        report['sources_seen']['ubuntu-24.04']['first_seen'] -= 25 * 3600
        sync.save_state(report)
        again = self.run_with(self.imp, [])
        self.assertEqual(again['templates'][0]['action'], 'built')
        self.assertEqual(again['sources_seen'], {})

    def test_force_and_canary_nodes_do_not_wait(self):
        self.assertEqual(self.run_with(self.imp, [], self.conf_with(SOURCE_DELAY_HOURS=0))['templates'][0]['action'], 'built')
        self.node.add(9013, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/old'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/new'}])
        self.assertEqual(self.run_with(imp, ['--force'])['templates'][0]['action'], 'built')

    def test_a_different_new_image_restarts_the_clock_and_stale_is_not_delayed(self):
        sync.save_state({'sources_seen': {'ubuntu-24.04': {'key': 'older-new.img', 'first_seen': 1}}})
        self.assertEqual(self.run_with(self.imp, [])['templates'][0]['action'], 'waiting')
        self.node.add(9020, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13', days_old=40))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        self.assertEqual(self.run_with(imp, ['debian-13'])['templates'][0]['action'], 'built')   # stale：不等


class ResourceAndHostTest(Base):
    def test_run_budget_stops_new_builds(self):
        for i, name in enumerate(('debian-13', 'debian-12', 'ubuntu-24.04')):
            self.node.add(9010 + i, name)
        imp = FakeImport(self.node, [{'name': n, 'url': 'https://x/' + n} for n in ('debian-13', 'debian-12', 'ubuntu-24.04')])
        clock = [time.time()]
        orig = imp.build_template

        def slow_build(*a, **kw):
            clock[0] += 3 * 3600
            return orig(*a, **kw)
        imp.build_template = slow_build
        with mock.patch.object(sync.time, 'time', lambda: clock[0]):
            report = self.run_with(imp, ['--max-builds', '0'])
        actions = [r['action'] for r in report['templates']]
        self.assertEqual(actions.count('built'), 2)
        self.assertEqual([r['detail'] for r in report['templates'] if r['action'] == 'deferred'], ['run budget exhausted'])
        self.assertIn('run budget exhausted', report['message'])

    def test_host_cpu_level_gates_el10(self):
        self.node.add(9010, 'rocky-10')
        imp = FakeImport(self.node, [{'name': 'rocky-10', 'url': 'https://r/10', 'min_cpu_level': 3},
                                     {'name': 'almaLinux-10', 'url': 'https://a/10', 'min_cpu_level': 3}])
        with mock.patch.object(pitlib, 'host_cpu_level', lambda path=None: 2):
            report = self.run_with(imp, ['--force', '--add-missing', '--storage', 'tank'])
        for row in report['templates']:
            self.assertEqual((row['action'], row['detail']), ('skipped', 'host CPU lacks x86-64-v3'))
        self.assertEqual((imp.built, report['failures']), ([], {}))

    def test_old_cpu_model_is_switched_to_host_for_el10(self):
        self.node.add(9010, 'rocky-10', cfg={'cpu': 'kvm64'})
        self.node.add(9011, 'rocky-9', cfg={'cpu': 'kvm64'})
        imp = FakeImport(self.node, [{'name': 'rocky-10', 'url': 'https://r/10', 'min_cpu_level': 3},
                                     {'name': 'rocky-9', 'url': 'https://r/9'}])
        report = self.run_with(imp, ['--force'])
        opts = {b[0]: b[4] for b in imp.built}
        self.assertEqual((opts['rocky-10']['cpu'], opts['rocky-9']['cpu']), ('host', 'kvm64'))
        row = {r['name']: r for r in report['templates']}['rocky-10']
        self.assertIn('cpu kvm64 cannot run x86-64-v3 guests; the new template uses host', row['warnings'])

    def test_appliance_failure_defers_the_rest_of_the_run(self):
        for i, name in enumerate(('debian-13', 'debian-12', 'ubuntu-24.04')):
            self.node.add(9010 + i, name)
        imp = FakeImport(self.node, [{'name': n, 'url': 'https://x/' + n} for n in ('debian-13', 'debian-12', 'ubuntu-24.04')],
                         fail={'debian-13': pitlib.LaunchError('libguestfs appliance failed to start')})
        report = self.run_with(imp, ['--max-builds', '0'])
        rows = {r['name']: r for r in report['templates']}
        self.assertEqual(rows['debian-13']['failure'], 'appliance')
        for name in ('debian-12', 'ubuntu-24.04'):
            self.assertEqual(rows[name]['action'], 'deferred')
            self.assertIn('libguestfs appliance cannot start', rows[name]['detail'])
        self.assertEqual(set(report['failures']), {'debian-13'})
        self.assertEqual(imp.built, [])

    def test_storage_that_cannot_hold_templates_is_skipped_before_download(self):
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        with mock.patch.object(pitlib, 'storage_config', lambda s: {'type': 'lvm', 'content': 'images'}):
            report = self.run_with(imp, ['debian-13', '--add-missing', '--storage', 'vg0'])
        row = report['templates'][0]
        self.assertEqual(row['action'], 'skipped')
        self.assertIn('storage vg0 (type lvm) cannot hold VM templates', row['detail'])
        self.assertEqual((imp.built, report['failures']), ([], {}))

    def test_cache_dir_from_config(self):
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        cache = os.path.join(self.tmp.name, 'cache')
        os.makedirs(cache)
        with open(os.path.join(cache, 'debian-13.img.download'), 'w') as fh:
            fh.write('half')
        self.run_with(imp, [], self.conf_with(CACHE_DIR=cache))
        self.assertEqual(imp.CACHE_DIR, cache)
        self.assertFalse(os.path.exists(os.path.join(cache, 'debian-13.img.download')))


class SwitchSafetyTest(Base):
    def test_lost_hookscript_blocks_the_switch_and_removes_the_new_build(self):
        self.node.add(9010, 'debian-13', cfg={'hookscript': 'local:snippets/hook.sh'})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        imp.drop_keys = ('hookscript',)
        report = self.run_with(imp, ['--force'])
        row = report['templates'][0]
        self.assertEqual(row['action'], 'failed')
        self.assertIn('not switching', row['detail'])
        self.assertEqual(self.node.vms[9010]['name'], 'debian-13')
        new = imp.built[0][2]
        self.assertNotIn(new, self.node.vms)          # 新建的半成品当场删掉

    def test_unexpected_hookscript_format_is_not_rebuilt(self):
        self.node.add(9010, 'debian-13', cfg={'hookscript': 'local:snippets/../hook.sh;x'})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, ['--force'])
        self.assertEqual(report['templates'][0]['action'], 'failed')
        self.assertIn('unexpected format', report['templates'][0]['detail'])
        self.assertEqual(imp.built, [])

    def test_keys_that_were_not_carried_over_are_reported(self):
        self.node.add(9010, 'debian-13', cfg={'sockets': '2', 'args': '-fw_cfg file=/x', 'numa': '1'})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, ['--force'])
        self.assertIn('not carried over from VM 9010: args, sockets', report['templates'][0]['warnings'])
        self.assertEqual(imp.extra_configs[-1].get('numa'), '1')

    def test_shared_cursor_is_read_and_written(self):
        self.node.add(9010, 'debian-13')
        pitlib.write_shared_high_water(9500)
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, ['--force'])
        self.assertEqual(report['templates'][0]['new_vmid'], 9501)
        self.assertEqual(pitlib.read_shared_high_water(), 9501)
        self.assertEqual(report['vmid_high_water'], 9501)

    def test_cursor_follows_a_wraparound(self):
        # 游标到顶回绕后要跟着回到低位：记 max 的话此后每次都从顶端回绕（= 取最小空闲号）
        self.node.add(9010, 'debian-13')
        sync.save_state({'vmid_high_water': 9999})
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, ['--force'])
        self.assertEqual(report['templates'][0]['new_vmid'], 9001)
        self.assertEqual(report['vmid_high_water'], 9001)


class CleanupAndGcTest(Base):
    def test_locked_leftovers(self):
        meta = {'v': 1, 'name': 'debian-13'}
        self.node.add(9040, 'debian-13-pitbuild', tags=['pit'], meta=meta, lock='create')
        self.node.add(9041, 'pitsmoke-9040', template=False, tags=['pit-smoke'], lock='clone')
        self.node.add(9042, 'ubuntu-24.04-pitbuild', tags=['pit'], meta={'v': 1, 'name': 'ubuntu-24.04'}, lock='backup')
        notes = sync.cleanup_stale_builds('pve1', self.conf, dry_run=False, cache_dir=self.tmp.name)
        self.assertNotIn(9040, self.node.vms)
        self.assertNotIn(9041, self.node.vms)
        self.assertIn(9042, self.node.vms)
        destroys = [c for c in self.node.commands if c[:2] == ['qm', 'destroy']]
        self.assertTrue(all(c[-2:] == ['--skiplock', '1'] for c in destroys))
        self.assertEqual(notes, [('ubuntu-24.04', 'leftover build VM 9042 (ubuntu-24.04-pitbuild) is locked (backup); left alone')])

    def test_gc_covers_every_yaml_name_not_just_selected_rows(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        self.retired_tpl(9020, 'debian-12', 30)
        self.retired_tpl(9021, 'debian-12', 20)
        self.retired_tpl(900, 'debian-12', 40)                  # 区间外：不碰
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-12', 'url': 'https://d/12'}])
        report = self.run_with(imp, ['debian-13'])
        self.assertEqual([r['name'] for r in report['templates']], ['debian-13'])
        self.assertEqual(report['gc']['debian-12']['deleted'], [9020])
        self.assertNotIn(9020, self.node.vms)
        self.assertIn(900, self.node.vms)

    def test_gc_runs_for_failed_rows_too(self):
        self.node.add(9010, 'debian-13')
        self.retired_tpl(9020, 'debian-13', 30)
        self.retired_tpl(9021, 'debian-13', 20)
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}], fail={'debian-13': pitlib.PitError('x')})
        row = self.run_with(imp, [])['templates'][0]
        self.assertEqual((row['action'], row['deleted']), ('failed', [9020]))


class ReportShapeTest(Base):
    def test_dry_run_plan_rows(self):
        self.node.add(9010, 'debian-13', cfg={'hookscript': 'local:snippets/hook.sh'})
        self.node.add(9011, 'debian-12', meta=self.fresh_meta('debian-12', 'https://d/12.qcow2'))
        self.node.add(9005, 'debian-12', meta=self.fresh_meta('debian-12', 'https://d/12.qcow2', days_old=3))
        self.node.add(9012, 'ubuntu-24.04', meta=self.fresh_meta('ubuntu-24.04', 'https://u/24.img'))
        self.retired_tpl(9020, 'ubuntu-24.04', 30)
        self.retired_tpl(9021, 'ubuntu-24.04', 20)
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13.qcow2'}, {'name': 'debian-12', 'url': 'https://d/12.qcow2'},
                                     {'name': 'ubuntu-24.04', 'url': 'https://u/24.img'}])
        report = self.run_with(imp, ['--dry-run'])
        rows = {r['name']: r for r in report['templates']}
        plan = rows['debian-13']
        self.assertEqual(plan['action'], 'would-build')
        self.assertEqual({k: plan[k] for k in ('storage', 'format', 'bridge', 'hookscript', 'next_vmid', 'source', 'reason')},
                         {'storage': 'local-lvm', 'format': None, 'bridge': 'vmbr1', 'hookscript': True, 'next_vmid': 9022,
                          'source': '13.qcow2', 'reason': 'legacy'})
        self.assertEqual((rows['debian-12']['action'], rows['debian-12']['would_retire']), ('would-retire', [9005]))
        self.assertEqual((rows['ubuntu-24.04']['action'], rows['ubuntu-24.04']['would_delete']), ('would-delete', [9020]))
        self.assertIn(9020, self.node.vms)
        self.assertEqual(set(sync.ACTIONS), set(report['summary']))
        self.assertEqual(report['summary']['would-delete'], 1)
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            sync.print_human(report)
        text = out.getvalue()
        self.assertIn('storage=local-lvm fmt=default bridge=vmbr1 hook=yes vmid≈9022 src=13.qcow2 reason=legacy would retire 9010', text)
        self.assertIn('would delete 9020', text)

    def test_report_has_the_new_top_level_fields_and_tolerates_old_state(self):
        sync.save_state({'failures': [], 'holds': 'garbage', 'vmid_high_water': None})   # 旧 / 坏状态文件
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13'))
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}])
        report = self.run_with(imp, [])
        for key in ('paused', 'holds', 'pending_forced', 'sources_seen', 'message', 'summary'):
            self.assertIn(key, report)
        self.assertEqual((report['paused'], report['holds'], report['failures']), (False, {}, {}))
        row = report['templates'][0]
        for key in ('storage', 'format', 'bridge', 'hookscript', 'next_vmid', 'source', 'reason'):
            self.assertIn(key, row)

    def test_list_mode_flags(self):
        self.node.add(9010, 'debian-13', meta=self.fresh_meta('debian-13', 'https://d/13.qcow2'), cfg={'hookscript': 'local:snippets/h.sh'})
        self.node.add(900, 'debian-13')                                              # 区间外
        self.node.add(9011, 'my-custom', lock='backup')                               # 不在 yaml
        self.node.add(9012, 'debian-10', cfg={'protection': '1'})
        self.node.add(9013, 'ubuntu-20.04')                                          # 已移进 retired（不再获取）
        self.retired_tpl(9020, 'debian-13', 30)
        self.retired_tpl(9021, 'debian-13', 20)
        imp = FakeImport(self.node, [{'name': 'debian-13', 'url': 'https://d/13'}, {'name': 'debian-10', 'url': 'https://d/10', 'eol': True}],
                         retired=['ubuntu-20.04'])
        out = io.StringIO()
        with mock.patch.object(sync, 'load_import_module', lambda: imp), mock.patch('sys.stdout', out):
            sync.list_mode('pve1', self.conf_with(SELECTOR='debian-13'), True)
        rows = {r['vmid']: r for r in json.loads(out.getvalue())['templates']}
        self.assertEqual(rows[900]['state'], 'out-of-range')
        self.assertEqual((rows[9010]['state'], rows[9010]['maintained'], rows[9010]['hookscript'], rows[9010]['storage']),
                         ('managed', True, True, 'local-lvm'))
        self.assertEqual((rows[9011]['in_yaml'], rows[9011]['lock']), (False, 'backup'))
        self.assertEqual((rows[9012]['eol'], rows[9012]['maintained'], rows[9012]['protection'], rows[9012]['state']),
                         (True, False, True, 'legacy'))
        self.assertEqual((rows[9013]['in_yaml'], rows[9013]['dropped_eol'], rows[9011]['dropped_eol']), (False, True, False))
        self.assertEqual((rows[9020]['reclaimable'], rows[9021]['reclaimable']), (True, False))   # 保留最新 1 个
        self.assertGreater(rows[9020]['retired_for_s'], 29 * 86400)


class ArgsTest(unittest.TestCase):
    def parse(self, argv):
        return sync.parse_args(argv)

    def test_trailing_double_dash(self):
        self.assertEqual(self.parse(['--json', '--']).selectors, [])
        self.assertEqual(self.parse(['--json', '--', 'debian-13']).selectors, ['debian-13'])

    def test_rollback_and_release_combinations(self):
        self.assertEqual(self.parse(['--rollback', 'debian-13', '--json']).rollback, 'debian-13')
        for bad in (['--rollback', 'debian-13', 'ubuntu-*'], ['--rollback', 'debian-13', '--force'],
                    ['--rollback', 'debian-13', '--add-missing'], ['--rollback', 'a', '--release', 'b'],
                    ['--release', 'debian-13', '--dry-run'], ['--rollback', '-x'], ['--rollback', 'a;b'],
                    ['--release', 'debian-13', 'x']):
            with self.assertRaises(SystemExit, msg=bad):
                with mock.patch('sys.stderr'):
                    self.parse(bad)

    def test_new_conf_keys_and_defaults(self):
        conf = sync.load_conf('/nonexistent', explicit=False)
        self.assertEqual({k: conf[k] for k in ('PAUSED', 'SOURCE_DELAY_HOURS', 'MIN_FREE_MEM_MB', 'SMOKE_DEEP',
                                               'RUN_BUDGET_HOURS', 'CACHE_DIR')},
                         {'PAUSED': '0', 'SOURCE_DELAY_HOURS': '24', 'MIN_FREE_MEM_MB': '4096', 'SMOKE_DEEP': '1',
                          'RUN_BUDGET_HOURS': '4', 'CACHE_DIR': ''})
        ok = pitlib.parse_conf_text('PAUSED=1\nSOURCE_DELAY_HOURS=0\nCACHE_DIR=/srv/pit-cache\n', sync.CONF_SCHEMA)
        self.assertEqual((ok['PAUSED'], ok['SOURCE_DELAY_HOURS'], ok['CACHE_DIR']), ('1', '0', '/srv/pit-cache'))
        for bad in ('PAUSED=2', 'SOURCE_DELAY_HOURS=1000', 'MIN_FREE_MEM_MB=-1', 'RUN_BUDGET_HOURS=100',
                    'CACHE_DIR=relative/path', 'CACHE_DIR=/tmp/$(id)', 'SMOKE_DEEP=yes'):
            with self.assertRaises(pitlib.PitError, msg=bad):
                pitlib.parse_conf_text(bad + '\n', sync.CONF_SCHEMA)


class ConfExampleTest(unittest.TestCase):
    def test_example_lists_every_key_with_the_default_value(self):
        path = os.path.join(test_sync.ROOT, 'pve-import-template.conf.example')
        with open(path) as fh:
            text = fh.read()
        self.assertEqual(pitlib.parse_conf_text(text, sync.CONF_SCHEMA), pitlib.parse_conf_text('', sync.CONF_SCHEMA))
        for key in sync.CONF_SCHEMA:
            self.assertRegex(text, r'(?m)^%s=' % key)


if __name__ == '__main__':
    unittest.main()
