#!/usr/bin/env bash
# install-timer.sh —— 安装 / 移除 / 查看每日自动 sync 的 systemd timer。
#
#   bash install-timer.sh install [--no-enable]   写单元（指向本目录）+ 缺省配置，启用 timer
#   bash install-timer.sh remove                  停用并删除单元（配置与状态文件保留）
#   bash install-timer.sh status                  timer 状态 + 最近一次 sync 结果
#
# 配置 /etc/pve-import-template.conf 已存在时永不覆盖（节点自己的选择）。输出英文（运维终端 / agent 日志直读）。
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${PIT_UNIT_DIR:-/etc/systemd/system}"
CONF="${PIT_CONF:-/etc/pve-import-template.conf}"
STATE="${PIT_STATE_DIR:-/var/lib/pve-import-template}/last-run.json"

die() { echo "ERROR: $*" >&2; exit 1; }

# 单元里的路径会被 systemd 当成可执行路径：只许常规字符，防止目录名里带空格 / 特殊字符把 ExecStart 拆坏
[[ "$DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "install directory '$DIR' contains unsupported characters"

cmd="${1:-status}"
shift || true

case "$cmd" in
  install)
    enable=1
    [ "${1:-}" = "--no-enable" ] && enable=0
    [ -f "$DIR/sync-templates.py" ] || die "sync-templates.py not found in $DIR"
    sed "s#@INSTALL_DIR@#$DIR#g" "$DIR/systemd/pve-template-sync.service.in" > "$UNIT_DIR/pve-template-sync.service.tmp"
    mv -f "$UNIT_DIR/pve-template-sync.service.tmp" "$UNIT_DIR/pve-template-sync.service"
    install -m 0644 "$DIR/systemd/pve-template-sync.timer" "$UNIT_DIR/pve-template-sync.timer"
    if [ ! -e "$CONF" ]; then
      install -m 0644 "$DIR/pve-import-template.conf.example" "$CONF"
      echo "created $CONF with defaults (edit PROFILE / STORAGE for this node)"
    fi
    systemctl daemon-reload
    if [ "$enable" = 1 ]; then
      systemctl enable --now pve-template-sync.timer
      echo "pve-template-sync.timer enabled"
    else
      echo "units installed; timer left as it was"
    fi
    ;;
  remove)
    systemctl disable --now pve-template-sync.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/pve-template-sync.service" "$UNIT_DIR/pve-template-sync.timer"
    systemctl daemon-reload
    echo "pve-template-sync units removed ($CONF and $STATE kept)"
    ;;
  status)
    systemctl list-timers pve-template-sync.timer --no-pager 2>/dev/null || true
    if [ -f "$STATE" ]; then
      python3 - "$STATE" <<'PY'
import json, sys, time
doc = json.load(open(sys.argv[1]))
when = doc.get('finished_at') or doc.get('started_at')
print('last run: %s  running=%s ok=%s%s  %s' % (
    time.strftime('%Y-%m-%d %H:%M', time.localtime(when)) if when else '-', doc.get('running'), doc.get('ok'),
    ' PAUSED' if doc.get('paused') else '',
    ', '.join('%s %d' % kv for kv in sorted((doc.get('summary') or {}).items()) if kv[1])))
if doc.get('message'):
    print('  %s' % doc['message'])
for name, hold in sorted((doc.get('holds') or {}).items()):
    print('  hold         %-24s since %s (%s)' % (name, time.strftime('%Y-%m-%d %H:%M', time.localtime(hold.get('at') or 0)),
                                                hold.get('by') or 'manual'))
for row in doc.get('templates') or []:
    if row.get('action') not in ('current', 'absent'):
        print('  %-12s %-24s %s' % (row.get('action'), row.get('name'), row.get('detail') or ''))
PY
    else
      echo "no sync has run yet"
    fi
    ;;
  *)
    die "usage: install-timer.sh install [--no-enable] | remove | status"
    ;;
esac
