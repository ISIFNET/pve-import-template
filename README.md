# pve-import-template

自动化导入 Cloud-init 镜像到 Proxmox VE 的工具。支持 Ubuntu、Debian、CentOS Stream、Rocky Linux、AlmaLinux、Fedora、openSUSE、Arch Linux、Alpine Linux 等主流发行版。

包含两个工具：

- **`import.py`** —— 下载云镜像、离线定制、创建并转换为 PVE 模板。
- **`update-templates.py`** —— 对**已经导入**的模板就地重新应用定制（网络调优 / qemu-guest-agent / 允许 root 登录等），**无需重新下载或重建模板**。

> 变更历史见 [CHANGELOG.md](./CHANGELOG.md)。

## 快速开始

### 一键安装（推荐）

```bash
wget -O - https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
# 或
curl https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

### 手动安装

```bash
apt install -y git
git clone https://github.com/ISIFNET/pve-import-template
cd pve-import-template
./setup.sh          # 添加 no-subscription 源并安装依赖
```

## import.py —— 导入模板

### 基本语法

```bash
python3 import.py <storage-name> <start-vmid> [template-name] [选项]
```

- `<storage-name>`：PVE 存储名称（如 `local-lvm`、`local`、`zfs-pool`）
- `<start-vmid>`：起始 VM ID，例如 900
- `[template-name]`：可选，指定模板（支持逗号分隔或通配符）

### 选项

| 选项 | 说明 |
|------|------|
| `--only-new` | 只导入 PVE 中尚不存在的模板 |
| `--refresh` | 忽略缓存，强制重新下载镜像 |
| `--mirror <name>` | 使用 `templates.yaml` 中配置的镜像源（内网环境） |
| `--no-tuning` | 本次导入不注入网络/内核 sysctl 调优（默认开启） |
| `--list` | 列出所有可用模板与镜像源后退出 |
| `-h, --help` | 显示帮助 |

### 使用示例

```bash
python3 import.py --list                                   # 查看可用模板
python3 import.py local-lvm 900                            # 导入全部（从 900 开始）
python3 import.py local-lvm 900 --only-new                 # 只导入还不存在的
python3 import.py local-lvm 900 ubuntu-22.04,ubuntu-20.04  # 指定多个
python3 import.py local-lvm 900 'ubuntu-*' --mirror tsinghua
python3 import.py local-lvm 900 ubuntu-22.04 --refresh
python3 import.py local-lvm 900 'rocky-*' --no-tuning
```

### 导入时的 VM 默认配置

- `--cpu host,flags=+aes`、`--ostype l26`
- `--agent enabled=1,fstrim_cloned_disks=1`（启用 QEMU Guest Agent，克隆后自动 fstrim）
- 系统盘 `virtio-scsi-single` + `discard=on,ssd=1`（支持 TRIM/精简回收）
- `--net0 virtio,bridge=vmbr0,queues=4`、`--serial0 socket`
- 启用 cloud-init 时：挂载 cloudinit 盘、`--ciuser root`、`--ipconfig0 ip=dhcp`

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

**选项**：`--vms`（允许选中已停止的普通 VM）、`--dry-run`（预演）、`-y/--yes`（跳过确认）、`--list`、`-h/--help`

### 示例

```bash
python3 update-templates.py --list                     # 列出模板及其系统盘
python3 update-templates.py --all --dry-run            # 预演：对所有模板应用网络调优
python3 update-templates.py --all                      # 给所有模板补网络调优
python3 update-templates.py --all --qga --permit-root  # 同时补 qga + 允许 root
python3 update-templates.py 'ubuntu-*' --no-tuning --qga
python3 update-templates.py 9000 9001 -y
```

> ⚠ `virt-customize` 会**就地**修改模板系统盘。若该模板已被**链接克隆（linked clone，常见于 lvmthin/zfs）**，
> 修改基卷可能影响这些克隆，请谨慎并建议先备份/快照。`--qga` 等联网安装动作要求宿主机可访问软件源。

## 支持的模板

| 系列 | 模板 |
|------|------|
| Ubuntu | ubuntu-18.04 / 20.04 / 22.04 / 24.04 / **26.04** |
| Debian | debian-10 / 11 / 12 / 13 |
| CentOS Stream | centos-stream-9 / 10（stream-8 已 EOL，默认注释） |
| Rocky Linux | **rocky-8 / 9 / 10** |
| AlmaLinux | almaLinux-8 / 9 / 10 |
| Fedora | **fedora-42 / 43** |
| openSUSE | **opensuse-leap-15.6** |
| 其他 | archLinux、alpineLinux-3.22 / 3.23 |

`python3 import.py --list` 可查看当前 `templates.yaml` 中的全部模板。

## 内置定制行为

镜像内的定制统一由 `uploads/` 下的可复用脚本完成（`import.py` 与 `update-templates.py` 共用，单一数据源）：

### 允许 root 登录（`permit-root-login.sh`）

所有模板默认允许 root 登录并启用密码认证（配合 `uploads/ssh.cfg`），便于初始化。

### qemu-guest-agent（`install-qga.sh`）

自动适配多发行版：Debian/Ubuntu（含 EOL，自动切 archive 源）、RHEL/CentOS/Rocky/Alma（dnf/yum/microdnf）、
openSUSE（zypper）、Arch（pacman）、**Alpine（apk + openrc）**。安装失败不会中断导入。

### 网络/内核调优（`apply-net-tuning.sh`，**默认对所有模板开启**）

写入 `/etc/sysctl.d/99-network-tuning.conf` 与 `/etc/modules-load.d/bbr.conf`，开机生效；不支持的内核参数会被自动忽略：

```ini
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

**按发行版差异**：Alpine（musl/openrc，模块加载与 sysctl 行为不同）默认**不**注入调优（`net_tuning: false`）。
- 关闭单个模板：在该模板下加 `net_tuning: false`
- 全局关闭：`--no-tuning`

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

编辑 `templates.yaml`，每个模板支持：

- `name`：模板名称
- `url`：镜像下载地址
- `cloud_init`：是否启用 cloud-init
- `net_tuning`：是否注入网络/内核调优（默认 `true`，设 `false` 关闭）
- `unpack`：解压命令（支持 `{dl}` 和 `{img}` 占位符）
- `customize`：
  - `uploads`：上传文件到镜像（`--upload`）
  - `run`：在镜像内执行宿主机脚本文件（`--run`，相对路径基于脚本目录）
  - `commands`：在镜像内执行的内联命令（`--run-command`）

## 清除已存在的 VM

```bash
for vmid in $(qm list | awk '$1 >= 9000 && $1 <= 9999 {print $1}'); do
  echo "Destroying VM $vmid"
  qm destroy $vmid --purge
done
```

## 许可证

本项目基于原始仓库 [balthild/pve-import-template](https://github.com/balthild/pve-import-template) 进行改进。
