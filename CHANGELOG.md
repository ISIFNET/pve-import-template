# Changelog

本文件记录对 pve-import-template 的主要改动。

## [Unreleased] - 2026-09-24

### 交互式向导与默认只导入主流系列

- **新增 `tui.py` 交互式导入向导**（`run.sh` 默认入口；也可 `python3 import.py --tui`）：
  依次选择存储（自动列出支持 `images` 的存储与剩余空间）→ 起始 VMID（占用检测）→ 系统模板
  （预设：主流系列 / 全部 / 按系列 / 逐个勾选，已存在的模板自动标记）→ 导入选项 → 软件源镜像 → 模板硬件
  （网桥自动检测 / 内存 / 核数）→ 确认页（显示等价 `import.py` 命令，可回到任一步修改）。
  有 `whiptail`/`dialog` 用对话框界面，否则回退纯文本菜单（`--text`）；上次选择保存在
  `~/.config/pve-import-template/last.json`；`curl | bash` 管道模式下自动接管 `/dev/tty`。
- **`import.py` 默认只导入主流系列**：`templates.yaml` 新增顶层 `default_families`
  （默认 `ubuntu debian centos alpine`）；不指定模板时只导入这些系列，`--all` 导入全部。
  模板选择器新增**系列名**匹配（`rocky,alpine`），`--list` 改为按系列分组显示并标出默认系列。
- **`import.py` 新选项**：`--bridge` / `--memory` / `--cores`（模板 VM 硬件，原先固定为 vmbr0 / 512 / 1）、
  `--dry-run`（只打印模板与 VMID 分配计划）、`--tui`。`--only-new` 会列出被跳过的模板，全部已存在时直接结束。
- **`update-templates.py` 安全强化**：新增链接克隆检测（扫描本节点 VM 配置中的 `base-<vmid>-disk`，
  `--list` / 目标清单 / 向导中标出，`--skip-cloned` 可跳过）；修改期间 `qm set --lock clone`、结束 `qm unlock`
  （`--no-lock` 关闭），已带锁的 VM 自动跳过；新增 `--reapply`（按 `templates.yaml` 同名模板的 `customize` 重新应用）；
  失败目标的 virt-customize 输出保存到 `/tmp/pve-import-template/update-<vmid>-<时间>.log`；显示每个目标与整体耗时。
- **换源临时脚本不再残留**：`import.py` / `update-templates.py` 生成的 `/tmp/mirror_setup_*.sh` 现在会在
  virt-customize 结束后（以及进程退出时）自动删除；原先的 `--delete` 只作用于镜像内路径。
- **`update-templates.py` 接入向导并完善**：`tui.py` 入口菜单新增「更新已导入模板」（`python3 tui.py update` /
  `update-templates.py --tui` / `bash run.sh update`）：勾选目标（含已停止的普通 VM）→ 动作 → 软件源镜像 → 高级选项 → 确认。
  新增 `--mirror <name>`（复用 `import.py` 的换源逻辑）；virt-customize 失败按 stderr 区分「appliance 起不来」与
  「该模板脚本出错」：前者才回退 TCG / 中止全部，后者只记失败并继续；结束列出失败目标并以非 0 退出；Ctrl-C 友好退出。
- **`import.py` 失败隔离与汇总**：单个模板下载失败 / `qm` 出错（或 Ctrl-C）时自动 `qm destroy` 清理半成品 VM，
  继续导入其余模板，结束时打印成功 / 跳过 / 失败汇总并以非 0 退出。
- **镜像缓存目录固定为脚本目录下的 `cloud_img/`**（原先取决于当前工作目录，换目录运行会重复下载），
  可用 `CLOUD_IMG_DIR` 环境变量改到更大的分区。
- **`run.sh` 支持 `update` 子命令**：`bash run.sh update` 打开更新向导，`-y update ...` 直接透传给 `update-templates.py`。
- **`run.sh` 重写**：管道模式先克隆/更新仓库再以仓库内脚本继续（保证使用最新 `run.sh`）；仓库内运行时
  自动 `git pull --ff-only`（有本地修改时跳过，`NO_UPDATE=1` 关闭），`run.sh` 自身更新后重新执行；
  依赖齐全时跳过 `setup.sh`（`FORCE_SETUP=1` 强制）；非 root 自动 `sudo`；有终端时进入向导，
  `-y` / 无终端时非交互导入（默认主流系列 + `--only-new`）；`--setup-only` 只准备环境；
  非 UTF-8 locale 自动设置 `LC_ALL=C.UTF-8`。嵌套虚拟化配置移到 `setup.sh`。

### setup.sh 兼容性与完整性

- 支持 PVE 7（bullseye）/ 8（bookworm）/ 9（trixie）；`VERSION_CODENAME` 缺失时从 `/etc/debian_version` 推断；
  非 root 自动 `sudo`；新增命令行选项 `--skip-repo` / `--skip-checks` / `--no-nested` / `--full-check` / `-h`。
- apt 源：注释掉失效的 `cdrom:` 源；已存在的 `.list` 形式 no-subscription 源也能识别（不重复添加）；
  Proxmox 仓库密钥缺失时自动从 `enterprise.proxmox.com` 下载；bullseye 写 `.list`，bookworm/trixie 写 deb822。
- 依赖：只安装缺失的包（重复运行更快），新增 `whiptail`、`curl`，可选 `libguestfs-xfs`，非 PVE 主机补 `qemu-utils`；
  `apt-get update` 失败时诊断 401 / NO_PUBKEY / DNS 并继续尝试安装，批量失败时逐个安装定位；
  apt 装不上 yaml/tqdm 时回退 pip；最终校验 `virt-customize` / `xz` / `unzip` 可用。
- 新增检查：`/dev/kvm` 是否存在（否则提示会回退 force_tcg）、`/boot/vmlinuz-*` 权限、缓存目录剩余空间（< 10 GiB 告警）、
  PVE 命令齐全性；时间未同步时先尝试 `chronyc makestep`；TLS 检查显示使用的代理并区分 DNS 失败；
  `--full-check` 运行 `libguestfs-test-tool`。结束时汇总所有告警。
- 嵌套虚拟化：识别 Intel/AMD 且支持 vmx/svm 才配置；已启用则不重载模块；有 VM 运行时不重载并提示重启后生效。

## [0.x] - 2026-09-15

### PVE 9 适配与安装脚本

- **`setup.sh` 重写，支持 PVE 9（Debian trixie）**：不再依赖 `lsb_release` / `add-apt-repository`
  （PVE 9 默认没有 `lsb_release`，`software-properties-common` 也非必需），改读 `/etc/os-release`，
  以 deb822 格式写入 `/etc/apt/sources.list.d/pve-no-subscription.sources`，并用
  `Signed-By` 指向 Proxmox keyring；已有同名源时跳过（幂等）。
- **无订阅时自动禁用 enterprise 源**：PVE 9 默认启用 `pve-enterprise.sources` / `ceph.sources`，
  未订阅会导致 `apt update` 返回 401 并让 `set -e` 脚本中止。现通过 `pvesubscription get`
  判断，仅在无有效订阅时把 `.list` 注释、`.sources` 加 `Enabled: false`。
- **新增依赖 `ca-certificates`、`xz-utils`**，安装后执行 `update-ca-certificates`。
- **安装后环境检查**：NTP 同步状态、到 `cloud.debian.org` 的 HTTPS 链路；失败时给出
  时间 / CA 证书 / 代理三类排障命令。`SKIP_REPO=1`、`SKIP_CHECKS=1`、`TLS_CHECK_URL` 可控制。
- **`run.sh`**：按 CPU 厂商选择 `kvm_intel` / `kvm_amd` 开启嵌套虚拟化；不再对整机
  `apt upgrade`（避免一键脚本顺带升级宿主机内核）；仅保证 `git` 可用后即交给 `setup.sh`。

### 下载

- **TLS 证书校验失败不再重试**：`[SSL: CERTIFICATE_VERIFY_FAILED]` 不是瞬时故障，`import.py`
  现在直接失败并提示检查系统时间 / `ca-certificates` / 代理。
- 新增 **`--insecure`**：下载镜像时跳过证书校验，作为环境暂时无法修复时的应急手段。

## [0.x] - 2026-07-02

### 修复

- **libguestfs `guestfs_launch failed` 自动回退**：当 KVM 不可用（如 PVE 本身是嵌套虚拟机）
  导致 appliance 启动失败时，`import.py` 与 `update-templates.py` 会自动用 `force_tcg`（软件模拟）
  重试一次。`update-templates.py` 另加 `--tcg`（全程强制软件模拟）、`--debug`（详细日志）、
  失败时打印排障提示，并对宿主机内核不可读 / 缺少 `/dev/kvm` 做预检提示。
  - 若 KVM 与软件模拟**都**失败（说明是宿主机 libguestfs 环境问题，会影响所有目标），
    `update-templates.py` 会**尽早中止**并跳过剩余目标，给出针对性修复步骤
    （`chmod 0644 /boot/vmlinuz-*`、`update-guestfs-appliance`、`libguestfs-test-tool` 诊断）。
- **`update-templates.py` 支持 LVM/LVM-thin 上的模板基卷**：PVE 的模板基卷（`base-*`）默认带
  「activation-skip」标志、处于未激活状态，`/dev/<vg>/<lv>` 设备节点不存在，导致
  `virt-customize: No such file or directory`。现在会在定制前用 `lvchange -ay -K` 激活、
  用完后 `lvchange -an` 恢复未激活状态（仅当本工具激活过它）；非 LVM 卷（qcow2/zvol）行为不变。

### 调优参数增强与分系统处理

- **`apply-net-tuning.sh` 改为按 init 系统分别处理**：
  - systemd 系（Debian/Ubuntu/RHEL/openSUSE/Arch）用 `/etc/modules-load.d/bbr.conf`；
  - 非 systemd（Alpine/openrc 等）改用 `/etc/modules` 并尽量启用 `sysctl` 服务。
  - 从目标系统 `/etc/os-release` 读取发行版信息（沙箱内可安全读取；不依赖沙箱内核版本）。
- **sysctl 参数集在原有基础上补充了与 BBR/大缓冲互补的稳健增强项**（不支持的内核会自动忽略）：
  `tcp_slow_start_after_idle=0`、`tcp_mtu_probing=1`、`tcp_fastopen=3`、
  `tcp_notsent_lowat=131072`、`net.core.netdev_max_backlog=16384`、`tcp_max_tw_buckets=262144`。
- 说明：对**已导入**的模板，运行 `python3 update-templates.py --all` 即可套用新参数。
- Alpine 仍默认 `net_tuning: false`（busybox `sysctl` 行为不一致更保守）；脚本已支持 openrc，
  如需可在该模板设 `net_tuning: true` 手动开启。

### 多节点集群支持

- **`update-templates.py` 默认只处理「本节点」的模板**：`pvesh` 在集群里会返回所有节点的 VM，
  而 `virt-customize`/`qm`/`pvesm` 只能操作本节点磁盘，之前会尝试更新其他节点的模板而失败。
  - 自动识别本节点名（`/etc/pve/local` 符号链接，回退短主机名）。
  - 新增 `--node <name>` 指定节点、`--all-nodes` 显式放开限制。
  - 按 VMID 指定到其他节点的模板时，给出「请到 X 节点运行」的可执行提示而非报错。
  - `--list` / 目标清单显示节点名与作用范围。
- **`import.py` 跨集群感知**：
  - `--only-new` 改为**跨节点**判断模板是否已存在。
  - **VMID 分配自动跳过集群内已占用的 ID**（集群内 VMID 全局唯一），避免与其他节点的 VM
    冲突导致 `qm create` 失败。

## [0.1.0] - 2026-06-30

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
