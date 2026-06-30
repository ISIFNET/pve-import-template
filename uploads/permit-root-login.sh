#!/bin/sh
# permit-root-login.sh —— 允许 root 通过 SSH 登录（多数 cloud 镜像默认禁用）
# 幂等：已有 PermitRootLogin 行则改为 yes，否则追加。永远 exit 0。
if grep -E '^PermitRootLogin\s' /etc/ssh/sshd_config >/dev/null 2>&1; then
  sed -i -E 's/^(PermitRootLogin\s.*)+$/PermitRootLogin yes # \1/' /etc/ssh/sshd_config
else
  printf "\nPermitRootLogin yes\n" >> /etc/ssh/sshd_config
fi
exit 0
