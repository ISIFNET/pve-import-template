# Changelog

本文件记录对 pve-import-template 的主要改动。

## [Unreleased] - 2026-06-30

### 新增

- **`update-templates.py`（新工具）**：对「已经导入 PVE 的模板」就地重新应用定制，
  无需重新下载镜像或重建模板。
  - 通过 `pvesm path` 解析模板系统盘 → 直接对磁盘运行 `virt-customize` 注入脚本。
  - 选择器：`<vmid>` / `<name>` / 通配 `'ubuntu-*'` / `--all`。
  - 动作：默认网络/内核调优；`--qga`、`--permit-root`、`--run <script>`（可重复）；`--no-tuning` 关闭默认调优。
  - 安全：`--dry-run` 预演、执行前确认（`-y` 跳过）、自动跳过未停止的普通 VM，
    并对「链接克隆可能受影响」给出告警。
  - `--list` 列出所有模板及识别到的系统盘；磁盘识别自动跳过 cloudinit / cdrom / EFI / TPM 卷，并遵循启动顺序。

- **网络/内核调优脚本 `uploads/apply-net-tuning.sh`（单一数据源）**：被 `import.py` 与
  `update-templates.py` 共用。落盘到 `/etc/sysctl.d/99-network-tuning.conf` + `/etc/modules-load.d/bbr.conf`，
  按需开机生效，不支持的内核参数会被自动忽略（脚本始终 `exit 0`）。

- **可复用定制脚本**：将原先内联在 `templates.yaml` 的命令抽出为独立脚本，供两个工具共用：
  - `uploads/permit-root-login.sh`、`uploads/install-qga.sh`。
  - `install-qga.sh` 新增 **Alpine（apk + openrc）** 支持。

- **`import.py` 新增 `customize.run`**：在镜像内执行宿主机脚本文件（`--run`），相对路径基于脚本目录解析。

- **`import.py` 新选项**：`--no-tuning`（本次不注入调优）、`--list`（列出模板与镜像源）、`-h/--help`（完整帮助）。

- **新增系统模板**：
  - Ubuntu **26.04 LTS**（resolute）
  - **Rocky Linux 8 / 9 / 10**
  - **Fedora 42 / 43**（Cloud Base）
  - **openSUSE Leap 15.6**

### 变更

- **sysctl 调优参数集更新**为面向高带宽时延积（BBR + 大缓冲）的推荐组合，并默认对所有模板开启：

  ```
  net.core.default_qdisc = fq
  net.core.rmem_max = 67108848
  net.core.wmem_max = 67108848
  net.core.somaxconn = 4096
  net.ipv4.tcp_max_syn_backlog = 4096
  net.ipv4.tcp_congestion_control = bbr
  net.ipv4.tcp_rmem = 16384 16777216 536870912
  net.ipv4.tcp_wmem = 16384 16777216 536870912
  net.ipv4.tcp_adv_win_scale = -2
  net.ipv4.tcp_sack = 1
  net.ipv4.tcp_timestamps = 1
  kernel.panic = -1
  vm.swappiness = 0
  ```

  > 调优现在「默认对所有模板开启」；不再需要在每个模板里手动声明。

- **按发行版差异化处理调优**：Alpine（musl/openrc，模块加载与 sysctl 行为不同）默认 **不** 注入调优
  （`net_tuning: false`）。任意模板均可用 `net_tuning: false` 单独关闭，或命令行 `--no-tuning` 全局关闭。

- **`templates.yaml` 结构精简**：移除内联 YAML 命令别名（`command_permit_root_login` /
  `command_install_qga` / `command_enable_bbr_optimized`），统一改为引用 `uploads/*.sh`，便于维护、去重。

- **导入时的 VM 默认配置优化**：
  - `--agent enabled=1,fstrim_cloned_disks=1`（启用 QEMU Guest Agent + 克隆后自动 fstrim）
  - `--ostype l26`（正确的 Linux 客户机类型）
  - 系统盘 `--scsihw virtio-scsi-single` + `discard=on,ssd=1`（支持 TRIM/精简回收）
  - cloud-init 默认 `--ipconfig0 ip=dhcp`（避免克隆后无网络）

### 健壮性

- `import.py` 从脚本所在目录加载 `templates.yaml` / `uploads/`，不再依赖当前工作目录。
- `qm list` 缺失（非 PVE 环境）时不再抛异常；`--help`/`--list` 在缺少依赖时也能使用。
- 未匹配到任何模板时给出友好提示而非静默结束。

### 备注 / 待关注

- **centos-stream-8** 已于 2024-05-31 EOL，镜像随时可能下线；默认已注释，需要可在 `templates.yaml` 取消注释。
- **Fedora** 镜像 URL 含构建号（无 `-latest` 软链），点版本更新后可能失效，请按需更新构建号。
- `mirrors` 暂未提供 openSUSE / Fedora 专用源映射；这两者用 `--mirror` 时会跳过换源（不影响导入）。
