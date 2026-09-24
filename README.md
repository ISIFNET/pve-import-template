# pve-import-template

自动化导入 Cloud-init 镜像到 Proxmox VE 的工具。支持 Ubuntu、Debian、CentOS Stream、Rocky Linux、AlmaLinux、Fedora、openSUSE、Arch Linux、Alpine Linux 等主流发行版。

包含三个工具：

- **`tui.py`** —— 交互式导入向导（`run.sh` 默认入口）：选存储、起始 VMID、系统模板与各项选项，最后调用 `import.py`。
- **`import.py`** —— 下载云镜像、离线定制、创建并转换为 PVE 模板。默认只导入主流系列（Ubuntu / Debian / CentOS Stream / Alpine），其余系列按需选择。
- **`update-templates.py`** —— 对**已经导入**的模板就地重新应用定制（网络调优 / qemu-guest-agent / 允许 root 登录等），**无需重新下载或重建模板**。

> 变更历史见 [CHANGELOG.md](./CHANGELOG.md)。

## 快速开始

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
# 或
wget -O - https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

`run.sh` 会克隆/更新仓库、按需运行 `setup.sh`，然后打开**交互式向导**（`tui.py`）：

1. 选择存储（自动列出支持 `images` 的存储及剩余空间）
2. 起始 VMID（已占用会提示并自动跳过）
3. 选择系统模板：预设「主流系列（Ubuntu / Debian / CentOS Stream / Alpine）」「全部」「按系列选择」「逐个勾选」，
   之后仍可逐个勾选微调；已存在的模板自动标记
4. 导入选项：跳过已存在、网络/内核调优、强制重下、跳过 TLS 校验
5. 软件源镜像：`templates.yaml` 里的 `aliyun / tsinghua / huawei / tencent / ustc` 或内网源
6. 模板硬件：网桥（自动检测）、内存、CPU 核数

确认页会显示**等价的 `import.py` 命令**，方便复制到脚本里；上一次的选择会记住（`~/.config/pve-import-template/last.json`）。
有 `whiptail`/`dialog` 时使用对话框界面，否则回退为纯文本菜单（`--text` 可强制）。

```bash
bash run.sh                         # 交互向导
bash run.sh local-lvm 9000          # 向导，预填存储 / 起始 VMID
bash run.sh -y                      # 非交互：主流系列 + --only-new，storage=local-lvm，vmid 从 9000 开始
bash run.sh -y tank 9000 --all      # 非交互：导入全部模板
bash run.sh --setup-only            # 只准备环境
curl -fsSL .../run.sh | bash -s -- -y local-lvm 9000
```

环境变量：`INSTALL_DIR`（管道模式的克隆目录）、`NO_UPDATE=1`（不 `git pull`）、`SKIP_SETUP=1` / `FORCE_SETUP=1`、`NONINTERACTIVE=1`。

### 手动安装

```bash
apt install -y git
git clone https://github.com/ISIFNET/pve-import-template
cd pve-import-template
./setup.sh          # 修复 apt 源、安装依赖、配置嵌套虚拟化并检查环境
python3 tui.py      # 或直接 python3 import.py <storage> <start-vmid> ...
```

`setup.sh` 支持 PVE 7（bullseye）/ 8（bookworm）/ 9（trixie），幂等可重复运行：

- 从 `/etc/os-release` / `/etc/debian_version` 识别版本，不依赖 `lsb_release`；
- 未检测到有效订阅时自动禁用 `enterprise.proxmox.com`（pve / ceph）源，否则 `apt update` 会因 401 失败；
  注释掉失效的 `cdrom:` 源；缺少 Proxmox 仓库密钥时自动下载；
- 添加 `pve-no-subscription` 源（bookworm/trixie 用 deb822 `.sources`，bullseye 用 `.list`；已存在则跳过）；
- 只安装缺失的依赖：`git python3-tqdm python3-yaml libguestfs-tools unzip xz-utils ca-certificates whiptail curl`
  （可选 `libguestfs-xfs`；非 PVE 主机额外 `qemu-utils`），`apt update` 失败时给出 401 / NO_PUBKEY / DNS 诊断；
- 按 CPU 厂商启用嵌套虚拟化（`kvm_intel` / `kvm_amd` 的 `nested=1`）；
- 检查 NTP 时间同步（未同步时尝试 `chronyc makestep`）、HTTPS 下载链路、`/dev/kvm` 与 libguestfs 前置条件、磁盘剩余空间，
  最后汇总需要留意的项。
- 选项 / 环境变量：`--skip-repo`(`SKIP_REPO=1`)、`--skip-checks`(`SKIP_CHECKS=1`)、`--no-nested`(`SKIP_NESTED=1`)、
  `--full-check`(`FULL_CHECK=1`，运行 `libguestfs-test-tool`)、`TLS_CHECK_URL`。

## tui.py —— 交互式向导

```bash
python3 tui.py [storage] [start-vmid] [template-selector] [import.py 的选项...] [--text] [--dry-run] [--no-save]
```

所有参数只作为向导的**默认值**（例如 `python3 tui.py tank 9000 --mirror aliyun`），最终以向导里的选择为准。

| 选项 | 说明 |
|------|------|
| `--text` | 强制纯文本菜单（不用 whiptail/dialog；也允许从管道喂入答案，便于脚本化） |
| `--dry-run` | 确认后只打印导入计划（透传给 `import.py --dry-run`） |
| `--no-save` | 不把本次选择写入 `~/.config/pve-import-template/last.json` |
| `-h` | 显示帮助 |

向导内：`Esc`/「返回」回到上一步；确认页可回到任一步修改，或「仅打印等价命令并退出」把命令拿去脚本里用。

## import.py —— 导入模板

### 基本语法

```bash
python3 import.py <storage-name> <start-vmid> [template-selector] [选项]
```

- `<storage-name>`：PVE 存储名称（如 `local-lvm`、`local`、`zfs-pool`）
- `<start-vmid>`：起始 VM ID，例如 900
- `[template-selector]`：可选，逗号分隔的模板名 / 通配符 / **系列名**（如 `rocky`、`alpine`）。
  不指定时只导入 `templates.yaml` 中 `default_families` 定义的主流系列（默认 `ubuntu debian centos alpine`），`--all` 导入全部。

### 选项

| 选项 | 说明 |
|------|------|
| `--all` | 导入 `templates.yaml` 中的全部模板（忽略 `default_families`） |
| `--only-new` | 只导入 PVE 中尚不存在的模板 |
| `--refresh` | 忽略缓存，强制重新下载镜像 |
| `--mirror <name>` | 使用 `templates.yaml` 中配置的镜像源（内网环境） |
| `--no-tuning` | 本次导入不注入网络/内核 sysctl 调优（默认开启） |
| `--insecure` | 下载镜像时跳过 TLS 证书校验（仅用于临时绕过证书问题） |
| `--bridge <name>` / `--memory <MB>` / `--cores <n>` | 模板 VM 的网桥 / 内存 / 核数（默认 `vmbr0` / 512 / 1） |
| `--dry-run` | 只打印导入计划（模板与分配的 VMID），不下载不创建 |
| `--list` | 按系列列出所有可用模板与镜像源后退出 |
| `--tui` | 启动交互式向导（等价于 `python3 tui.py`） |
| `-h, --help` | 显示帮助 |

### 使用示例

```bash
python3 import.py --list                                   # 按系列查看可用模板
python3 import.py local-lvm 900                            # 导入主流系列（从 900 开始）
python3 import.py local-lvm 900 --all                      # 导入全部
python3 import.py local-lvm 900 --only-new                 # 只导入还不存在的
python3 import.py local-lvm 900 ubuntu-22.04,ubuntu-20.04  # 指定多个
python3 import.py local-lvm 900 rocky,alma                 # 按系列
python3 import.py local-lvm 900 'ubuntu-*' --mirror tsinghua
python3 import.py local-lvm 900 ubuntu-22.04 --refresh
python3 import.py local-lvm 900 'rocky-*' --no-tuning
python3 import.py tank 900 --bridge vmbr1 --memory 1024 --cores 2 --dry-run
```

### 运行行为

- **镜像缓存**：下载的镜像保存在脚本目录下的 `cloud_img/`（不随当前工作目录变化），可用环境变量 `CLOUD_IMG_DIR` 指到更大的分区；
  再次导入时用 ETag / Last-Modified 比对，未变化则不重新下载，`--refresh` 强制重下并在导入后删除缓存。
- **逐个隔离失败**：某个模板下载失败或 `qm` 命令出错时，会清理该 VMID 的半成品 VM，继续导入其余模板，
  结束时打印「成功 / 跳过 / 失败」汇总并以非 0 退出；修复原因后加 `--only-new` 重跑即可，已成功的自动跳过。
- **`Ctrl-C`** 中断时同样会清理当前未完成的 VM。

### 导入时的 VM 默认配置

- `--cpu host,flags=+aes`、`--ostype l26`、`--memory 512`、`--cores 1`（可用 `--memory` / `--cores` 覆盖）
- `--agent enabled=1,fstrim_cloned_disks=1`（启用 QEMU Guest Agent，克隆后自动 fstrim）
- 系统盘 `virtio-scsi-single` + `discard=on`（支持 TRIM/精简回收，不伪装为 SSD）
- `--net0 virtio,bridge=vmbr0,queues=4`（网桥可用 `--bridge` 覆盖）、`--serial0 socket`
- 启用 cloud-init 时：挂载 cloudinit 盘、`--ciuser root`、`--ipconfig0 ip=dhcp`

> **集群**：`--only-new` 跨节点判断模板是否已存在；分配 VMID 时自动跳过集群内已占用的 ID
> （VMID 全局唯一），避免与其他节点的 VM 冲突而 `qm create` 失败。

## update-templates.py —— 更新已导入的模板

给**已经存在**的模板补充或更新设置，直接对模板系统盘运行 `virt-customize`，不重下镜像、不改 VMID。

```bash
python3 update-templates.py [选择器 ...] [动作] [选项]
```

**选择器**：`<vmid>` / `<name>` / 通配 `'ubuntu-*'` / `--all`（所有模板）

**动作**（不指定动作时默认仅「网络调优」）：

| 动作 | 说明 |
|------|------|
| `--tuning` / `--no-tuning` | 应用 / 不应用网络/内核调优（默认应用） |
| `--qga` | 安装并启用 qemu-guest-agent |
| `--permit-root` | 允许 root SSH 登录 |
| `--run <script>` | 运行任意宿主机脚本（可重复） |

**选项**：`--vms`（允许选中已停止的普通 VM）、`--node <name>`、`--all-nodes`、`--dry-run`（预演）、`--tcg`（强制软件模拟）、`--debug`（libguestfs 详细日志）、`-y/--yes`（跳过确认）、`--list`、`-h/--help`

### 示例

```bash
python3 update-templates.py --list                     # 列出（本节点）模板及其系统盘
python3 update-templates.py --all --dry-run            # 预演：对本节点所有模板应用网络调优
python3 update-templates.py --all                      # 给本节点所有模板补网络调优
python3 update-templates.py --all --qga --permit-root  # 同时补 qga + 允许 root
python3 update-templates.py 'ubuntu-*' --no-tuning --qga
python3 update-templates.py 9000 9001 -y
```

> **多节点集群**：`virt-customize`/`qm`/`pvesm` 只能操作**本节点**的磁盘。集群里 `pvesh` 会返回所有节点的 VM，
> 因此本工具**默认只处理本节点的模板**；要更新其他节点上的模板，请到对应节点分别运行（或用 `--node`）。
>
> **LVM/LVM-thin 模板**：PVE 模板基卷（`base-*`）默认未激活，工具会在定制前 `lvchange -ay -K` 激活、
> 用完后 `lvchange -an` 恢复；无需手动处理。
>
> **`guestfs_launch failed`**：多因 KVM 不可用（如 PVE 本身是嵌套虚拟机）。工具会自动回退 `force_tcg`
> 软件模拟重试；仍失败可加 `--tcg` 全程软件模拟、`--debug` 看详细日志，或 `chmod 0644 /boot/vmlinuz-*`。
>
> ⚠ `virt-customize` 会**就地**修改模板系统盘。若该模板已被**链接克隆（linked clone，常见于 lvmthin/zfs）**，
> 修改基卷可能影响这些克隆，请谨慎并建议先备份/快照。`--qga` 等联网安装动作要求宿主机可访问软件源。

## 支持的模板

| 系列 | 模板 |
|------|------|
| Ubuntu（默认） | ubuntu-18.04 / 20.04 / 22.04 / 24.04 / **26.04** |
| Debian（默认） | debian-10 / 11 / 12 / 13 |
| CentOS Stream（默认） | centos-stream-9 / 10（stream-8 已 EOL，默认注释） |
| Alpine Linux（默认） | alpineLinux-3.22 / 3.23 / 3.24 |
| Rocky Linux | rocky-8 / 9 / 10 |
| AlmaLinux | almaLinux-8 / 9 / 10 |
| Fedora | fedora-42 / 43 / 44 |
| openSUSE | opensuse-leap-15.6 / 16.0 |
| Arch Linux | archLinux |

标注「默认」的系列由 `templates.yaml` 的 `default_families` 决定：不指定模板时 `import.py` 只导入这些系列，向导默认勾选这些系列，
其余系列在向导中按需勾选或用 `--all` / 系列名导入。`python3 import.py --list` 可按系列查看全部模板。

## 内置定制行为

镜像内的定制统一由 `uploads/` 下的可复用脚本完成（`import.py` 与 `update-templates.py` 共用，单一数据源）：

### 允许 root 登录（`permit-root-login.sh`）

所有模板默认允许 root 登录并启用密码认证（配合 `uploads/ssh.cfg`），便于初始化。

### qemu-guest-agent（`install-qga.sh`）

自动适配多发行版：Debian/Ubuntu（含 EOL，自动切 archive 源）、RHEL/CentOS/Rocky/Alma（dnf/yum/microdnf）、
openSUSE（zypper）、Arch（pacman）、**Alpine（apk + openrc）**。安装失败不会中断导入。

### 网络/内核调优（`apply-net-tuning.sh`，**默认对所有模板开启**）

写入 `/etc/sysctl.d/99-network-tuning.conf`，开机生效；不支持的内核参数会被自动忽略。

**分 init 系统处理**（脚本从目标系统 `/etc/os-release` 判断，不依赖 libguestfs 沙箱内核）：
- **systemd 系**（Debian/Ubuntu/RHEL/Rocky/Alma/Fedora/openSUSE/Arch）：模块写 `/etc/modules-load.d/bbr.conf`
- **非 systemd**（Alpine/openrc 等）：模块写 `/etc/modules`，并尽量启用 `sysctl` 服务

参数（核心 BBR + 大缓冲，及互补的稳健增强项）：

```ini
# 核心：BBR + 大缓冲（高带宽时延积）
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
# 增强：与 BBR/大缓冲互补（不支持的内核自动忽略）
net.ipv4.tcp_slow_start_after_idle = 0   # 长连接空闲后不回退拥塞窗口
net.ipv4.tcp_mtu_probing = 1             # 缓解 PMTU 黑洞
net.ipv4.tcp_fastopen = 3                # TCP Fast Open（客户端+服务端）
net.ipv4.tcp_notsent_lowat = 131072      # 配合 BBR 降低 bufferbloat/延迟
net.core.netdev_max_backlog = 16384      # 高 pps 入队缓冲
net.ipv4.tcp_max_tw_buckets = 262144     # TIME_WAIT 上限
```

**按发行版差异**：Alpine（musl/openrc，busybox `sysctl` 行为不一致）默认**不**注入调优（`net_tuning: false`）；
脚本已支持 openrc，如需可在该模板设 `net_tuning: true` 开启。
- 关闭单个模板：在该模板下加 `net_tuning: false`；全局关闭：`--no-tuning`
- **对已导入的模板套用新参数**：`python3 update-templates.py --all`（本节点）

### Alpine DNS

Alpine 模板上传预置 DNS（`uploads/resolv.conf`，1.1.1.1 / 8.8.8.8），解决默认 DNS 注入问题。

## 镜像源配置（内网环境）

`--mirror <name>` 可在导入时自动换源。内置：`aliyun`、`tsinghua`、`huawei`、`tencent`、`ustc`。
支持的系统：Ubuntu、Debian、RHEL/CentOS/Rocky/Alma、Alpine、Arch（openSUSE/Fedora 暂未提供专用源映射，使用时会跳过换源）。

可在 `templates.yaml` 的 `mirrors` 段添加自定义内网源：

```yaml
mirrors:
  internal:
    ubuntu:
      url: http://your-internal-mirror.local/ubuntu
    debian:
      url: http://your-internal-mirror.local/debian
    rhel:
      url: http://your-internal-mirror.local/centos
```

## 支持的存储类型

- 目录类型：dir、nfs、glusterfs
- 块设备类型：zfspool、lvm、lvmthin

## 自定义模板

编辑 `templates.yaml`。顶层 `default_families` 列出默认导入的系列；每个模板支持：

- `name`：模板名称（系列由名称前缀自动推断：`ubuntu-24.04` → `ubuntu`、`almaLinux-9` → `alma`）
- `family`：可选，显式指定系列名
- `url`：镜像下载地址
- `cloud_init`：是否启用 cloud-init
- `net_tuning`：是否注入网络/内核调优（默认 `true`，设 `false` 关闭）
- `unpack`：解压命令（支持 `{dl}` 和 `{img}` 占位符）
- `customize`：
  - `uploads`：上传文件到镜像（`--upload`）
  - `run`：在镜像内执行宿主机脚本文件（`--run`，相对路径基于脚本目录）
  - `commands`：在镜像内执行的内联命令（`--run-command`）

## 下载镜像时报 `CERTIFICATE_VERIFY_FAILED`

例如 `certificate verify failed: certificate has expired`。这不是镜像站的问题，通常是宿主机环境：

1. **系统时间不准**：`date -R` 核对；`systemctl enable --now chrony && chronyc makestep`。
2. **CA 证书过期/缺失**：`apt-get install --reinstall ca-certificates && update-ca-certificates`。
3. **代理拦截 HTTPS**：检查 `http_proxy` / `https_proxy` 环境变量与 `/etc/apt/apt.conf.d/` 中的代理设置。

运行 `./setup.sh` 会自动做上述检查。确认环境无法修复时，可临时加 `--insecure` 跳过校验。

## 清除已存在的 VM

```bash
for vmid in $(qm list | awk '$1 >= 9000 && $1 <= 9999 {print $1}'); do
  echo "Destroying VM $vmid"
  qm destroy $vmid --purge
done
```

## 许可证

本项目基于原始仓库 [balthild/pve-import-template](https://github.com/balthild/pve-import-template) 进行改进。
