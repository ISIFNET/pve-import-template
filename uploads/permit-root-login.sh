#!/bin/sh
# permit-root-login.sh —— 允许 root 通过 SSH 登录（多数 cloud 镜像默认禁用）
#
# sshd 对同一个关键字**首条生效**：
#   - 有 `Include /etc/ssh/sshd_config.d/*.conf`（OpenSSH 8.2+ 的发行版默认如此，且 Include 在文件顶部）：
#     写一个排在最前的 drop-in（00-），不改主配置；
#   - 没有 Include（较老的发行版）：注释掉已有的 PermitRootLogin，把新的一行放到文件**最前面**
#     （追加在末尾可能落进 Match 块，或被前面的同名行盖掉）。
# 不在配置行尾写 `# 注释`（旧版 OpenSSH 会报 garbage at end of line，sshd 起不来）。幂等。永远 exit 0
# （是否生效由 verify-image.sh 用 sshd -T 核对）。
CONF=/etc/ssh/sshd_config
DROPIN_DIR=/etc/ssh/sshd_config.d
DROPIN="$DROPIN_DIR/00-pve-template-root.conf"

# openSUSE Leap 16 / Tumbleweed：发行版配置在 /usr/etc/ssh/sshd_config（/etc 下没有主配置），它 Include /etc/ssh/sshd_config.d
if [ ! -f "$CONF" ] && [ -f /usr/etc/ssh/sshd_config ]; then
  CONF=/usr/etc/ssh/sshd_config
fi
[ -f "$CONF" ] || exit 0

if grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/' "$CONF"; then
  mkdir -p "$DROPIN_DIR"
  printf '# Managed by pve-import-template: allow root login (password auth is controlled by cloud-init ssh_pwauth)\nPermitRootLogin yes\n' > "$DROPIN"
  chmod 0644 "$DROPIN"
  exit 0
fi

# 没有 Include 却是 /usr/etc 的发行版配置：不改 /usr（包升级会覆盖），复制一份到 /etc 再改（/etc 优先生效）
if [ "$CONF" = /usr/etc/ssh/sshd_config ]; then
  cp "$CONF" /etc/ssh/sshd_config
  CONF=/etc/ssh/sshd_config
fi

TMP="$CONF.pit.$$"
{
  echo "PermitRootLogin yes"
  # 已有的「PermitRootLogin yes」（含上次插入的那行）删掉，其它取值注释掉
  sed -E '/^PermitRootLogin yes$/d; s/^([[:space:]]*PermitRootLogin[[:space:]])/#\1/' "$CONF"
} > "$TMP" && cat "$TMP" > "$CONF"
rm -f "$TMP"
exit 0
