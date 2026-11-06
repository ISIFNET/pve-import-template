# -*- coding: utf-8 -*-
"""uploads/ 注入脚本的行为测试：在临时 PATH 里放假的包管理器跑脚本（不碰宿主机的 apt / dnf）。"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPGRADE = os.path.join(ROOT, 'uploads', 'upgrade-packages.sh')

FAKE_PM = '''#!/bin/sh
# 前 FAIL_TIMES 次失败，之后成功；每次调用的参数记到 $CALLS
n=0
[ -f "$COUNTER" ] && read n < "$COUNTER"
n=$((n + 1))
echo "$n" > "$COUNTER"
echo "$(basename "$0") $*" >> "$CALLS"
[ "$n" -gt "$FAIL_TIMES" ]
'''


class UpgradeScriptTest(unittest.TestCase):
    def run_with(self, tool, fail_times):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        fake = os.path.join(d, tool)
        with open(fake, 'w') as fh:
            fh.write(FAKE_PM)
        os.chmod(fake, 0o755)
        with open(os.path.join(d, 'sleep'), 'w') as fh:     # 重试间隔不真睡
            fh.write('#!/bin/sh\nexit 0\n')
        os.chmod(os.path.join(d, 'sleep'), 0o755)
        for real in ('tr', 'basename'):
            os.symlink(shutil.which(real), os.path.join(d, real))
        env = {'PATH': d, 'COUNTER': os.path.join(d, 'n'), 'CALLS': os.path.join(d, 'calls'), 'FAIL_TIMES': str(fail_times)}
        proc = subprocess.run(['/bin/sh', UPGRADE], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with open(env['CALLS']) as fh:
            calls = fh.read().splitlines()
        return proc.returncode, calls

    def test_dnf_upgrade_is_retried_like_apt(self):
        code, calls = self.run_with('dnf', 2)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['dnf -y upgrade --refresh'] * 3)
        code, calls = self.run_with('dnf', 5)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)

    def test_yum_update_is_retried(self):
        code, calls = self.run_with('yum', 1)
        self.assertEqual(code, 0)
        self.assertEqual(calls, ['yum -y update'] * 2)

    def test_apt_does_not_force_phased_updates(self):
        with open(UPGRADE) as fh:
            code = [l for l in fh.read().splitlines() if not l.lstrip().startswith('#')]
        self.assertFalse([l for l in code if 'Always-Include-Phased-Updates' in l])
        self.assertTrue([l for l in code if l.strip().startswith('APT_OPTS=')])


if __name__ == '__main__':
    unittest.main()
