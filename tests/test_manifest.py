# -*- coding: utf-8 -*-
"""manifest.py：平台 vendoring 用的模板清单。"""

import datetime
import importlib.util
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location('pitmanifest', os.path.join(ROOT, 'manifest.py'))
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)
imp = manifest.load_import_module()


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.doc = manifest.build_manifest(imp, imp.load_config())
        self.by_name = {t['name']: t for t in self.doc['templates']}

    def test_top_level(self):
        with open(os.path.join(ROOT, 'VERSION')) as fh:
            self.assertEqual(self.doc['tool_version'], fh.read().strip())
        self.assertEqual(self.doc['default_profile'], 'standard')
        self.assertEqual(set(self.doc['profiles']), {'minimal', 'standard', 'full'})
        self.assertIn('rocky-9', self.doc['profiles']['minimal'])
        self.assertEqual(len(self.doc['profiles']['full']), len(self.doc['templates']))
        self.assertEqual(list(self.doc), ['tool_version', 'default_profile', 'profiles', 'templates'])

    def test_display_names(self):
        expect = {'debian-13': 'Debian 13', 'ubuntu-24.04': 'Ubuntu 24.04', 'centos-stream-10': 'CentOS Stream 10',
                  'alpineLinux-3.24': 'Alpine Linux 3.24', 'opensuse-leap-16.0': 'openSUSE Leap 16.0',
                  'archLinux': 'Arch Linux', 'almaLinux-10': 'AlmaLinux 10', 'rocky-9': 'Rocky Linux 9'}
        for name, display in expect.items():
            self.assertEqual(self.by_name[name]['display_name'], display, name)
        self.assertIsNone(self.by_name['archLinux']['version'])
        self.assertEqual(self.by_name['centos-stream-10']['family'], 'centos')
        self.assertEqual(self.by_name['centos-stream-10']['family_label'], 'CentOS Stream')

    def test_eol_and_cpu_level(self):
        self.assertIs(self.by_name['debian-10']['eol'], True)
        self.assertEqual(self.by_name['debian-13']['eol'], '2030-06-30')
        self.assertIsNone(self.by_name['archLinux']['eol'])
        self.assertEqual(self.by_name['rocky-10']['min_cpu_level'], 3)
        self.assertIsNone(self.by_name['rocky-9']['min_cpu_level'])
        for t in self.doc['templates']:
            self.assertEqual(set(t), {'name', 'family', 'family_label', 'version', 'display_name', 'eol', 'min_cpu_level'})

    def test_overrides_and_odd_yaml_values(self):
        cfg = {'templates': [{'name': 'debian-13', 'display_name': 'Debian 13 (Trixie)', 'eol': datetime.date(2030, 6, 30)},
                             {'name': 'ubuntu-26.04', 'eol': False, 'min_cpu_level': '2'}],
               'profiles': {'tiny': ['debian-13']}}
        doc = manifest.build_manifest(imp, cfg)
        a, b = doc['templates']
        self.assertEqual((a['display_name'], a['eol']), ('Debian 13 (Trixie)', '2030-06-30'))
        self.assertEqual((b['eol'], b['min_cpu_level']), (None, 2))
        self.assertEqual(doc['profiles'], {'tiny': ['debian-13']})
        self.assertEqual(doc['default_profile'], 'standard')

    def test_cli_prints_only_json(self):
        proc = subprocess.run([sys.executable, os.path.join(ROOT, 'manifest.py')], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, cwd='/')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), json.loads(json.dumps(self.doc)))


if __name__ == '__main__':
    unittest.main()
