# pve-import-template

自动化导入 Cloud-init 镜像到 Proxmox VE 的工具。支持 Ubuntu、Debian、CentOS Stream、Arch Linux、Alpine Linux、AlmaLinux 等主流发行版。

## 快速开始

### 一键安装（推荐）

使用 wget：
```bash
wget -O - https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

或使用 curl：
```bash
curl https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

### 手动安装

1. 安装 git 并克隆仓库：
```bash
apt install -y git
git clone https://github.com/ISIFNET/pve-import-template
cd pve-import-template
```

2. 安装依赖（添加 no-subscription 源并安装所需软件包）：
```bash
./setup.sh
```

## 使用方法

### 基本语法

```bash
python3 import.py <storage-name> <start-vmid> [template-name] [选项]
```

**参数说明：**
- `<storage-name>`：PVE 存储名称（如 `local-lvm`、`local`、`zfs-pool` 等）
- `<start-vmid>`：起始 VM ID，例如 900
- `[template-name]`：可选，指定要导入的模板（支持逗号分隔或通配符）
- `[选项]`：可选参数，见下方说明

### 选项说明

- `--only-new`：只导入 PVE 中尚不存在的模板
- `--refresh`：强制刷新，忽略缓存重新下载镜像
- `--mirror <name>`：使用指定的镜像源（适用于内网环境）

### 使用示例

**导入所有模板（从 VMID 900 开始）：**
```bash
python3 import.py local-lvm 900
```

**只导入不存在的模板：**
```bash
python3 import.py local-lvm 900 --only-new
```

**导入指定的多个模板：**
```bash
python3 import.py local-lvm 900 ubuntu-22.04,ubuntu-20.04
```

**使用通配符导入一批模板：**
```bash
python3 import.py local-lvm 900 'ubuntu-*'
python3 import.py local-lvm 900 'debian-*'
python3 import.py local-lvm 900 'almaLinux-*'
```

**强制刷新指定模板：**
```bash
python3 import.py local-lvm 900 ubuntu-22.04 --refresh
```

**使用内网镜像源：**
```bash
# 使用阿里云镜像源
python3 import.py local-lvm 900 --mirror aliyun

# 使用清华大学镜像源
python3 import.py local-lvm 900 --mirror tsinghua

# 导入 Ubuntu 系列模板并使用华为云镜像源
python3 import.py local-lvm 900 'ubuntu-*' --mirror huawei
```

**组合使用选项：**
```bash
python3 import.py local-lvm 900 'ubuntu-*' --only-new
python3 import.py local-lvm 900 'ubuntu-*' --only-new --mirror aliyun
```

## 支持的模板

### Ubuntu
- ubuntu-18.04（Bionic Beaver）
- ubuntu-20.04（Focal Fossa）
- ubuntu-22.04（Jammy Jellyfish）
- ubuntu-24.04（Noble Numbat）

### Debian
- debian-10（Buster）
- debian-11（Bullseye）
- debian-12（Bookworm）
- debian-13（Trixie）

### CentOS Stream
- centos-stream-8
- centos-stream-9
- centos-stream-10

### AlmaLinux
- almaLinux-8
- almaLinux-9
- almaLinux-10

### 其他发行版
- archLinux
- alpineLinux-3.22
- alpineLinux-3.23

## 特殊配置

### Alpine Linux DNS 配置

Alpine Linux 3.23 模板包含预配置的 DNS 服务器（1.1.1.1 和 8.8.8.8），以解决默认 DNS 注入问题。配置文件位于 `uploads/resolv.conf`。

### SSH 配置

所有模板都预配置了以下 SSH 设置（通过 `uploads/ssh.cfg`）：
- 允许 root 登录
- 支持密码认证（用于初始化配置）

### qemu-guest-agent

脚本会自动尝试安装 qemu-guest-agent，支持：
- Debian/Ubuntu（包括 EOL 版本，自动切换到 archive 源）
- RHEL/CentOS/AlmaLinux/Rocky（dnf/yum/microdnf）
- openSUSE（zypper）
- Arch Linux（pacman）

安装失败不会中断导入流程，确保兼容性。

### 网络优化

部分模板（Ubuntu 22.04+、Debian 10+）预配置了：
- TCP BBR 拥塞控制算法
- TCP SYN cookies 保护
- ARP 防欺骗配置
- 其他网络调优参数

配置文件：`/etc/sysctl.d/99-network-tuning.conf`

## 镜像源配置（内网环境）

对于网络受限的内网环境，可以使用 `--mirror` 参数指定镜像源，在导入模板时自动配置软件包源。

### 内置镜像源

工具内置了以下国内镜像源：

| 名称 | 说明 |
|------|------|
| `aliyun` | 阿里云镜像源 |
| `tsinghua` | 清华大学镜像源 |
| `huawei` | 华为云镜像源 |
| `tencent` | 腾讯云镜像源 |
| `ustc` | 中国科学技术大学镜像源 |

### 使用方法

```bash
# 使用阿里云镜像源导入所有模板
python3 import.py local-lvm 900 --mirror aliyun

# 使用清华镜像源导入 Ubuntu 模板
python3 import.py local-lvm 900 'ubuntu-*' --mirror tsinghua
```

### 自定义镜像源

您可以在 `templates.yaml` 中的 `mirrors` 部分添加自定义镜像源：

```yaml
mirrors:
  # 自定义内网镜像源
  internal:
    ubuntu:
      url: http://your-internal-mirror.local/ubuntu
    debian:
      url: http://your-internal-mirror.local/debian
    rhel:
      url: http://your-internal-mirror.local/centos
    alpine:
      url: http://your-internal-mirror.local/alpine
    arch:
      url: http://your-internal-mirror.local/archlinux
```

### 支持的操作系统

镜像源配置支持以下操作系统类型：

- **Ubuntu**：自动替换 `archive.ubuntu.com` 和 `security.ubuntu.com`
- **Debian**：自动替换 `deb.debian.org` 和 `security.debian.org`
- **RHEL/CentOS/AlmaLinux**：自动配置 yum/dnf 仓库
- **Alpine Linux**：自动替换 `dl-cdn.alpinelinux.org`
- **Arch Linux**：自动配置 pacman mirrorlist

## 支持的存储类型

- **目录类型**：dir、nfs、glusterfs
- **块设备类型**：zfspool、lvm、lvmthin

## 自定义模板

您可以编辑 `templates.yaml` 来添加或修改模板配置。模板配置支持：

- `name`：模板名称
- `url`：镜像下载地址
- `cloud_init`：是否启用 cloud-init
- `unpack`：解压命令（支持 `{dl}` 和 `{img}` 占位符）
- `customize`：定制配置
  - `uploads`：上传文件到镜像
  - `commands`：在镜像中执行的命令

## 清除存在的VM

for vmid in $(qm list | awk '$1 >= 9000 && $1 <= 9999 {print $1}'); do
  echo "Destroying VM $vmid"
  qm destroy $vmid --purge
done

## 许可证

本项目基于原始仓库 [balthild/pve-import-template](https://github.com/balthild/pve-import-template) 进行改进。
