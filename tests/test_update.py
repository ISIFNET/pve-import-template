# -*- coding: utf-8 -*-
"""update-templates.py：--all 默认跳过 sync-templates.py 托管的模板（pit tag）。"""

import importlib.util
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location('pveupdate', os.path.join(ROOT, 'update-templates.py'))
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)

VMS = [
    {'vmid': 9001, 'name': 'debian-13', 'template': 1, 'status': 'stopped', 'node': 'pve1', 'tags': ['pit', 'pit-b20261106']},
    {'vmid': 9002, 'name': 'debian-12-retired-202611010000', 'template': 1, 'status': 'stopped', 'node': 'pve1',
     'tags': ['pit', 'pit-retired']},
    {'vmid': 9003, 'name': 'my-golden', 'template': 1, 'status': 'stopped', 'node': 'pve1', 'tags': []},
    {'vmid': 100, 'name': 'VM-100', 'template': 0, 'status': 'stopped', 'node': 'pve1', 'tags': []},
]


class SelectTargetsTest(unittest.TestCase):
    def test_all_skips_managed_templates_by_default(self):
        self.assertEqual([v['vmid'] for v in upd.select_targets(VMS, [], True, False)], [9003])
        self.assertEqual([v['vmid'] for v in upd.select_targets(VMS, [], True, False, include_managed=True)], [9001, 9002, 9003])

    def test_explicit_selectors_still_work(self):
        self.assertEqual([v['vmid'] for v in upd.select_targets(VMS, ['9001'], False, False)], [9001])
        self.assertEqual([v['vmid'] for v in upd.select_targets(VMS, ['debian-*'], False, False)], [9001, 9002])

    def test_parse_args_and_tags_from_pvesh(self):
        self.assertTrue(upd.parse_args(['--all', '--include-managed'])['include_managed'])
        self.assertFalse(upd.parse_args(['--all'])['include_managed'])
        out = b'[{"type":"qemu","vmid":9001,"name":"debian-13","template":1,"status":"stopped","node":"pve1","tags":"pit;pit-b20261106"}]'
        with mock.patch.object(upd.subprocess, 'check_output', return_value=out):
            self.assertEqual(upd.list_vms()[0]['tags'], ['pit', 'pit-b20261106'])


if __name__ == '__main__':
    unittest.main()
