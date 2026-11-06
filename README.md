# pve-import-template

自动化导入 Cloud-init 镜像到 Proxmox VE 的工具。支持 Ubuntu、Debian、CentOS Stream、Rocky Linux、AlmaLinux、Fedora、openSUSE、Arch Linux、Alpine Linux 等主流发行版。

包含四个工具（外加给平台用的 `manifest.py`）：

- **`sync-templates.py`** —— **让模板保持最新（推荐的日常方式）**：发现上游新镜像 / 构建过期 / 注入方式变化时，
  **新建**一个模板、构建完全成功后才接管名字，旧模板退役保留一代用于回滚，过宽限期且无链接克隆后再删。
  非交互、JSON 输出、节点级锁，可由 systemd timer 每日运行（`bash run.sh timer install`）或由 rhapsi 平台下发。
- **`tui.py`** —— 交互式向导（`run.sh` 默认入口）：入口菜单选「导入新模板」或「更新已导入模板」，一步步选择后调用下面两个工具。
- **`import.py`** —— 下载云镜像、离线定制、创建并转换为 PVE 模板。不指定模板时导入 `templates.yaml` 的默认配置组（`@standard`），其余按需选择。
- **`update-templates.py`** —— 对**已经导入**的模板就地重新应用定制（网络调优 / qemu-guest-agent / 允许 root 登录等）。
  就地修改模板基卷对链接克隆有风险，换新构建请优先用 `sync-templates.py`；`--all` 默认跳过 sync 托管的模板（带 `pit` tag）。
- **`manifest.py`** —— 把 `templates.yaml` 整理成一份 JSON 清单（模板名、系列、版本、显示名、支持期、CPU 要求、配置组），
  平台 vendoring 时据此生成 `templates.manifest.json`：`python3 manifest.py > templates.manifest.json`。

> **由 rhapsi-agent 安装的节点**（代码在 `/opt/pve-import-template`）只带无人值守需要的文件，**没有 `run.sh` / `tui.py`**：
> 文中的 `bash run.sh sync …` / `python3 sync-templates.py …` 在这类节点上直接写成
> `python3 /opt/pve-import-template/sync-templates.py …`（例如 `python3 /opt/pve-import-template/sync-templates.py --rollback debian-13`）；
> `bash run.sh timer …` 对应目录里的 `install-timer.sh`（若随 agent 部署了的话）。

### 每次构建都会做的事（import / sync 共用）

1. 下载上游镜像（缓存保持**上游原样**，定制只在副本上做）；配了 `checksum` 的核对 SHA256/SHA512，配了 `discover` 的自动找最新点版本；
   主站失败按 `image_mirrors` 试备用站；下载有超时与重试，失败分类（404 / 网络 / TLS / 校验 / 空间）。
2. 镜像内：换源（可选）→ **包升级到最新安全补丁**（`upgrade-packages.sh`，失败即构建失败）→ ssh / root 登录 / qga →
   网络调优 → 写 `/etc/pve-template-release` → **注入校验**（`verify-image.sh`：qga 已装且自启、`sshd -T` 生效配置允许 root）
   → **封装清理**（`finalize-image.sh`：SSH host key、machine-id、cloud-init 状态、随机种子、日志、包缓存、shell 历史）。
3. `qm create` + `import-from` 一步导入挂盘，打 tags（`pit` / `pit-bYYYYMMDD`）并在 description 写构建元数据
   （来源 URL、sha256、校验结果、配方指纹、工具版本）——平台据此显示每台节点上模板的构建日期。

## sync-templates.py —— 让模板保持最新

```bash
python3 sync-templates.py --dry-run                 # 看计划：哪些模板会重建、为什么
python3 sync-templates.py                           # 按需重建（默认每轮最多 3 个，只处理本节点已有的模板）
python3 sync-templates.py alpine --force            # 立即重建本节点所有 Alpine 模板（安全事件）
python3 sync-templates.py @minimal --add-missing --storage local-lvm   # 小硬盘节点：只要 minimal 组，缺的补上
python3 sync-templates.py --list [--json]           # 本节点模板：在役 / 退役 / 旧工具导入（legacy）/ 区间外、构建时间、来源、能否回收
python3 sync-templates.py --rollback debian-13      # 新构建有问题：换回上一个退役版本并 hold 住（见下文「回滚」）
python3 sync-templates.py --release debian-13       # 解除 hold
bash run.sh timer install                           # 每日 03:30 起随机延迟 3h 自动 sync（配置 /etc/pve-import-template.conf）
bash run.sh timer status                            # 最近一次结果
```

**什么时候重建**（按优先级，`--max-builds` 限流，其余记 `deferred` 下一轮再建）：`forced` → `source-changed`（上游新镜像）
→ `stale`（超过 `MAX_AGE_DAYS`，默认 30 天，拿包升级里的安全补丁）→ `recipe-changed`（templates.yaml 条目 / 注入脚本 / 构建流程版本变了；
`eol` / `display_name` / `min_cpu_level` 不算）→ `legacy`（旧工具导入、没有元数据）→ `missing`（仅 `--add-missing`）。
`--force` 的模板若被名额 / 时间预算 / 内存推迟，记进 `pending_forced`，之后不带 `--force` 的运行接着当 `forced` 建（照样遵守失败退避），建成才清除。
上游新镜像首次见到后要满 `SOURCE_DELAY_HOURS`（默认 24）才采用，期间显示 `waiting`（见下文「金丝雀」）。

**切换与回收**：新模板先以 `<name>-pitbuild` 构建并转成模板，**开机冒烟**通过（从它链接克隆一台不接网卡的临时 VM，
`SMOKE_TIMEOUT` 秒内 qemu-guest-agent 必须应答——包升级在 chroot 里动了内核 / grub / extlinux，只有真开机才知道还能不能起；
`SMOKE_DEEP=1`（默认）时还要在同一预算内等到 `cloud-init status` 为 done / disabled（degraded 也算过，error 或超时仍 running 算失败），
并核对 SSH host key 已生成、`sshd -t` 通过、sshd 服务或 socket 在跑，agent 太老不支持 `guest-exec` 只记告警；用完即销毁）
后才改名为 `<name>`，再把旧模板改名 `<name>-retired-<退役那一刻的 UTC 时间>` 并打 `pit-retired`（两步之间两个同名模板都可克隆，没有空窗；
旧模板正被克隆时改名会撞锁，退避重试约 5 分钟，仍不行下一轮补退役）。每个模板保留最新 `KEEP_RETIRED`（默认 1）个退役版本用于回滚；
更早的在**退役满 12 小时、没有链接克隆引用、没开 protection、没被锁、带 `pit-retired` tag** 时才删除（手工起名的 `*-retired-*` 永远不动）。
只管理 VMID 在 `VMID_MIN`-`VMID_MAX` 之内的模板；新模板 VMID **单调往上分配**（不回填回收掉的号——号被别的系统复用，只记了 VMID 的
平台服务会装错系统）：游标取本节点状态文件与集群共享文件 `/etc/pve/pit-vmid-hw` 的较大者、分配后写回（写失败不致命），
在 `qm create` 前一刻才分配，`already exists` / `File exists` 撞号换号重试，存储上已有同号残留卷的号直接跳过。
切换前读回新模板的实际配置：旧模板有 hookscript 而新模板没有或不同就不切换（旧模板 hookscript 格式不认识则根本不建）；
沿用的节点策略键：`hookscript` `vga` `machine` `nameserver` `searchdomain` `cicustom` `ciupgrade` `numa` `tablet` `rng0` `scsihw` `ostype` `args`
（逐个正则白名单，`protection` 不沿用），其余旧模板有而新模板没有的键列在结果行 `warnings` 里。任何一步失败旧模板原样在役。

**上游是否变化**：discover 找到更高的点版本（只往上走：落后的备用站 / 旧缓存给出的更低版本不算）；同一地址有校验文件时比上游 hash
（Fedora / openSUSE / Arch 的 HEAD 会被重定向到不同镜像站，ETag 不可靠），没有才比 ETag / Last-Modified。下载的镜像必须是
qcow2 / raw 且不带 backing file / 外部 data-file（`qemu-img info` 核对），单个镜像上限 8 GiB。

**失败处理**：单模板失败隔离；同一配方连续失败按 `BACKOFF_HOURS`（默认 20h，配合每日 timer）翻倍退避（封顶 7 天），上游 404 固定退避 7 天，
被中断（timer 超时 / 关机）固定 2 小时；改了 templates.yaml（url / discover / 配方）立即重试；`--force` 无视退避。
UEFI（OVMF）模板、宿主机 CPU 达不到 `min_cpu_level`（EL10 要 x86-64-v3）的模板、`ADD_MISSING` 时目标存储放不了模板（厚置备 lvm / iscsi 等）记 `skipped`（不算失败）。
**资源不够记 `deferred`、不算失败、不进退避**：可用内存低于 `MIN_FREE_MEM_MB`（构建前、冒烟前各查一次）、写入后缓存目录剩不到 max(10 GiB, 10%)、
目标存储剩不到 max(20 GiB, 10%)、lvmthin 的 thin pool 元数据 ≥ 80%、本轮已超过 `RUN_BUDGET_HOURS`、libguestfs appliance 起不来（第一个失败，其余推迟）。
被强杀留下的构建半成品 / 冒烟 VM（带 clone / create 锁的也会 `--skiplock` 删，别的锁只记备注）/ 工作副本 / 下载到一半的 `.img.download`
由下一轮清掉（逐条核对 pit tag 与构建元数据，不只看名字）；构建失败后只删这次才出现在存储上的 `vm-<id>-*` / `base-<id>-*` 残留卷。
回收对本节点所有带 `pit-retired` 的 templates.yaml 名字执行（不只本轮处理过的）。结果写 `/var/lib/pve-import-template/last-run.json`，
日志在 `/var/log/pve-import-template/`（0700）。退出码：0 成功 / 1 有失败 / 2 用法或依赖 / 3 另一个实例在跑 /
4 集群没有 quorum（整轮跳过，状态写 `ok=false` + `message`，不记失败）/ 130 被中断。

**节点配置** `/etc/pve-import-template.conf`（见 `pve-import-template.conf.example`，KEY=VALUE、不经 shell）：
`PROFILE` / `SELECTOR`（维护哪些）、`ADD_MISSING` + `STORAGE`、`BRIDGE`、`ADOPT_LEGACY`、`MAX_AGE_DAYS`、`MAX_BUILDS`、`KEEP_RETIRED`、
`KEEP_CACHE`（默认 0：构建完删缓存镜像）、`CACHE_DIR`、`UPGRADE`、`NET_TUNING`、`MIRROR`、`VMID_MIN/MAX`（默认 9001-9999）、`BACKOFF_HOURS`、
`SMOKE_TEST` / `SMOKE_TIMEOUT` / `SMOKE_DEEP`、`PAUSED`、`SOURCE_DELAY_HOURS`、`MIN_FREE_MEM_MB`（默认 4096）、`RUN_BUDGET_HOURS`（默认 4）。

**小硬盘节点**：`PROFILE=minimal`（debian-13 / debian-12 / ubuntu-24.04 / ubuntu-22.04 / rocky-9 / alpineLinux-3.23）+ `KEEP_RETIRED=0`
+ `KEEP_CACHE=0`；构建前会检查缓存目录与目标存储的剩余空间（含上面的保留量），不够就记 `deferred`（`not enough space: …`）而不是写满磁盘。

### 回滚、hold、暂停、金丝雀

**回滚**：新构建上线后发现有问题（客户反馈开机慢、某个包坏了），在该节点上：

```bash
python3 sync-templates.py --rollback debian-13 [--json]
```

在本节点 VMID 区间内找最新的、带 `pit-retired`、名字形如 `debian-13-retired-<UTC>`、没被锁的退役模板，改回 `debian-13` 并去掉
`pit-retired`（其它 tag 保留），再把当前在役的同名模板按标准流程退役（先恢复再退役，没有空窗；退役失败会撤销恢复）。没有可恢复的版本时
退出码 1。回滚后该模板进入 **hold**：`stale` / `recipe-changed` / `legacy` / 同一个坏源的 `source-changed` / pending forced 一律跳过
（行 `action=held`），`--force` 也不越过；**上游出了新镜像**（文件名变了，或同一地址校验和变了）时自动解除（行 detail 注明
`hold released: new upstream image`），或手动 `--release debian-13`。所以退役版本至少要留一个（`KEEP_RETIRED>=1`），否则过了 12 小时
宽限期就没有可回滚的了。

**暂停**：`PAUSED=1` 写进 `/etc/pve-import-template.conf`，timer / 平台触发的 sync 拿到锁后什么都不做（退出码 0，状态文件写
`"paused": true` 与 `message`，退避 / hold / pending 原样保留）；`--rollback` / `--release` / `--list` / `--dry-run` 照常可用。
节点维护、存储迁移、排查问题时先暂停，改回 `PAUSED=0` 即恢复。

**金丝雀**：机队里挑一两台节点设 `SOURCE_DELAY_HOURS=0`，它们发现上游新镜像当天就重建；其余节点保持默认 24 小时，新镜像首次见到后
满 24 小时才采用（期间行 `action=waiting`，detail 写明「见到多久 / 还要多久」）。金丝雀上的冒烟失败或客户反馈有问题时，在金丝雀上
`--rollback`、在其余节点上暂停或等上游修复——坏镜像不会在同一天铺满整个机队。只影响「上游变了」，过期 / 配方变化 / legacy 不等；`--force` 不等。

**备份任务要排除 9001-9999 的模板**：PVE 的备份任务（vzdump / PBS）若按「全部 VM」选，会把模板也备份进去——每次重建换一个 VMID，
PBS 上就多一个永远不再增长的备份组；备份进行时模板带 `backup` 锁，恰好撞上的退役改名会失败（下一轮补退役）、GC 也会跳过它。
在 Datacenter → Backup 里改成按池 / 排除 9001-9999（或 `VMID_MIN`-`VMID_MAX`），模板丢了用 sync 重建即可，不需要备份。

### 状态文件 / --json 报告

`/var/lib/pve-import-template/last-run.json` 与 `--json` 输出同一结构（缺字段的旧文件按空处理）。顶层：`ok` / `running` / `started_at` /
`finished_at` / `version` / `node` / `dry_run` / `paused` / `message`（跳过原因：暂停、没有 quorum、中断、appliance 起不来、时间预算用完）/
`summary`（每个 action 的计数，没有的记 0）/ `failures`（退避记录）/ `holds`（`{name: {at, by, bad_vmid, bad_source, restored_vmid, bad_url, bad_checksum}}`）/
`pending_forced`（`{name: {at}}`）/ `sources_seen`（`{name: {key, first_seen}}`）/ `vmid_high_water`（本节点最近分配的模板 VMID）/ `gc`（未选中名字的回收结果）/
`templates`（每个模板一行）。行字段：`name` `action` `reason`（原因代码）`detail` `old_vmids` `storage` `format` `bridge` `hookscript`（有无）
`next_vmid`（would-build 时按游标预估）`source`（上游文件名）`warnings`，建成的另有 `new_vmid` `retired` `built_at` `source_url` `checksum_verified`，
回收有 `deleted` / `would_delete` / `gc_notes`。行 `action` 词表：`built` `current` `failed` `deferred` `backoff` `absent` `held` `waiting` `skipped`
`would-build` `would-retire` `would-delete` `rolled-back`。

> 变更历史见 [CHANGELOG.md](./CHANGELOG.md)。

## 快速开始

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
# 或
wget -O - https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

`run.sh` 会克隆/更新仓库、按需运行 `setup.sh`，然后打开**交互式向导**（`tui.py`）。入口菜单选「导入新模板」后：

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
bash run.sh update                  # 更新已导入模板的向导（见下文 update-templates.py）
bash run.sh -y update --all --qga   # 非交互更新：等价 python3 update-templates.py --all --qga -y
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
python3 tui.py                                   # 入口菜单：导入新模板 / 更新已导入模板
python3 tui.py import [storage] [start-vmid] [template-selector] [import.py 的选项...]
python3 tui.py update [选择器...] [update-templates.py 的选项...]
```

所有参数只作为向导的**默认值**（例如 `python3 tui.py tank 9000 --mirror aliyun`、`python3 tui.py update 'ubuntu-*' --qga`），
最终以向导里的选择为准。「更新」向导 4 步：勾选目标模板（含已停止的普通 VM）→ 动作（调优 / qga / root 登录）→ 软件源镜像 →
高级选项（额外脚本 / 跳过有链接克隆的模板 / 加锁 / `--tcg` / `--debug`），目标列表会标出链接克隆数，
确认页同样显示等价的 `update-templates.py` 命令。

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
- `[template-selector]`：可选，逗号分隔的模板名 / 通配符 / **系列名**（如 `rocky`、`alpine`）/ **配置组**（`@minimal`）。
  不指定时导入 `templates.yaml` 的默认配置组 `default_profile`（默认 `@standard`：在售发行版的受支持版本，不含 EOL），`--all` 导入全部。

### 选项

| 选项 | 说明 |
|------|------|
| `--all` | 导入 `templates.yaml` 中的全部模板（含 EOL，忽略 `default_profile`） |
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
| `--mirror <name>` | 把模板内的软件源换成 `templates.yaml` 中的镜像源（与 `import.py --mirror` 相同逻辑，按模板名识别发行版） |
| `--reapply` | 按 `templates.yaml` 中同名模板的 `customize`（uploads / run / commands）重新应用一遍 |
| `--run <script>` | 运行任意宿主机脚本（可重复） |

**选项**：`--vms`（允许选中已停止的普通 VM）、`--include-managed`（`--all` 也处理 sync 托管、带 `pit` tag 的模板；默认跳过）、`--skip-cloned`（跳过有链接克隆的模板）、`--no-lock`（修改期间不加 PVE 锁）、
`--node <name>`、`--all-nodes`、`--dry-run`（预演）、`--tcg`（强制软件模拟）、`--debug`（libguestfs 详细日志）、
`-y/--yes`（跳过确认）、`--list`（含链接克隆数 / 锁状态）、`--tui`（交互式向导）、`-h/--help`

**安全措施**：
- **链接克隆检测**：扫描本节点所有 VM 的磁盘配置，找出基于目标模板的链接克隆（`base-<vmid>-disk`），在目标清单里标出并提示；
  `--skip-cloned` 可自动跳过这类模板。
- **修改期间加锁**：对每个目标执行 `qm set --lock clone`，结束后 `qm unlock`，防止修改中途被克隆 / 备份 / 迁移；
  已经带锁（如 `lock=backup`）的 VM 会被跳过。若脚本被强杀留下锁，手动 `qm unlock <vmid>` 即可。
- **失败隔离**：某个模板的定制脚本出错只影响该模板（继续处理其余目标，输出保存到 `/tmp/pve-import-template/update-<vmid>-<时间>.log`，
  结束时列出失败目标并以非 0 退出）；只有 libguestfs appliance 本身起不来（KVM 与软件模拟都失败）才会中止全部并打印排障步骤。
- 每个目标与整体都会显示耗时；换源用的宿主机临时脚本在结束时自动清理。

### 示例

```bash
python3 update-templates.py --list                     # 列出（本节点）模板及其系统盘
python3 update-templates.py --tui                      # 交互式选择目标与动作
python3 update-templates.py 'ubuntu-*' --mirror tsinghua --qga   # 换源后再装 qga
python3 update-templates.py --all --reapply --skip-cloned        # 重新应用 templates.yaml 定制，跳过有链接克隆的
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
> **链接克隆**：`qm clone` 默认在 lvmthin / zfs / dir(qcow2) 上产生链接克隆，它们共享模板基卷。修改基卷后，
> 已有克隆的运行状态一般不受影响（基卷内容对它们是只读的快照/backing），但仍属于「共享底层被改写」，
> 建议用 `--list` 先看一眼，重要环境用 `--skip-cloned` 或先做备份。
>
> **`guestfs_launch failed`**：多因 KVM 不可用（如 PVE 本身是嵌套虚拟机）。工具会自动回退 `force_tcg`
> 软件模拟重试；仍失败可加 `--tcg` 全程软件模拟、`--debug` 看详细日志，或 `chmod 0644 /boot/vmlinuz-*`。
>
> ⚠ `virt-customize` 会**就地**修改模板系统盘。若该模板已被**链接克隆（linked clone，常见于 lvmthin/zfs）**，
> 修改基卷可能影响这些克隆，请谨慎并建议先备份/快照。`--qga` 等联网安装动作要求宿主机可访问软件源。

## 支持的模板

| 系列 | 模板 |
|------|------|
| Ubuntu（默认） | ubuntu-18.04 (EOL) / 20.04 (EOL) / 22.04 / 24.04 / **26.04** |
| Debian（默认） | debian-10 (EOL) / 11 (EOL) / 12 / 13 |
| CentOS Stream（默认） | centos-stream-9 / 10（自动找最新带日期构建） |
| Alpine Linux（默认） | alpineLinux-3.22 / 3.23 / 3.24（自动找最新点版本） |
| Rocky Linux | rocky-8 / 9 / 10 |
| AlmaLinux | almaLinux-8 / 9 / 10 |
| Fedora | fedora-42 (EOL) / 43 / 44 / **45**（自动找当前构建号） |
| openSUSE | opensuse-leap-15.6 (EOL) / 16.0 |
| Arch Linux | archLinux |

默认导入哪些由 `templates.yaml` 的 `default_profile` 决定（默认 `standard`）：不指定模板时 `import.py` 导入这个配置组，向导默认勾选它，
sync 的 `ADD_MISSING` 在没写 `PROFILE` / `SELECTOR` 时也只补它。

| 配置组 | 内容 | 适用 |
|---|---|---|
| `minimal` | debian-13 / 12、ubuntu-24.04 / 22.04、rocky-9、alpineLinux-3.23（6 个） | 小硬盘 / 边缘节点 |
| `standard`（默认） | debian-13 / 12、ubuntu-26.04 / 24.04 / 22.04、rocky-10 / 9 / 8、almaLinux-10 / 9 / 8、centos-stream-10 / 9、alpineLinux-3.24 / 3.23（15 个） | 常规节点 |
| `full` | 全部（含 EOL、Fedora、openSUSE、Arch） | 按需 |

组里只放安全支持期内的版本（测试钉死）。`eol` 写成日期（按 [endoflife.date](https://endoflife.date) 的安全支持截止日），到期后 sync 自动
不再补这个模板、不按年龄重建；届时把它移出组、换上新版本即可。`python3 import.py --list` 按系列列出全部模板（`*` = 默认配置组，含支持期 / EOL / 自动发现 / 校验标记）。
2026-11-05 全部 27 个地址与校验文件实测可用。

## 内置定制行为

镜像内的定制统一由 `uploads/` 下的可复用脚本完成（`import.py` 与 `update-templates.py` 共用，单一数据源）：

### 允许 root 登录（`permit-root-login.sh`）

所有模板默认允许 root 登录并启用密码认证（配合 `uploads/ssh.cfg`），便于初始化。sshd 对同一关键字首条生效：
有 `Include sshd_config.d/*.conf` 的发行版写排在最前的 drop-in `00-pve-template-root.conf`，老发行版把设置放到主配置第一行
（旧版追加在末尾，可能落进 `Match` 块；行尾注释在旧 OpenSSH 上会让 sshd 起不来）。是否生效由 `verify-image.sh` 用 `sshd -T` 核对。

### qemu-guest-agent（`install-qga.sh`）

自动适配多发行版：Debian/Ubuntu（先按自带源安装，**只有源确实不可用（已归档）才**切 archive / old-releases 并关闭 Valid-Until 校验；
旧版对所有 Ubuntu 无条件改到 old-releases，22.04 因此装不上 qga）、RHEL/CentOS/Rocky/Alma（dnf/yum/microdnf）、
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
支持的系统：Ubuntu、Debian、Rocky / AlmaLinux / CentOS Stream、Alpine、Arch（openSUSE/Fedora 暂未提供专用源映射，使用时会跳过换源）。
EL 系按发行版各配一条（`rocky` / `almalinux` / `centos-stream`，指到镜像站对应的那层目录；Stream 9/10 的 repo 文件只有 metalink，
会按仓库名改写成 baseurl）——三家的仓库路径不同，旧的 `rhel` 条目（`…/centos`）套到 Rocky / Alma / Stream 上会 404，只配了 `rhel`
的镜像站对这三家不换源（保持官方源）。`huawei` 暂未配 EL 条目（路径未核实）。

可在 `templates.yaml` 的 `mirrors` 段添加自定义内网源：

```yaml
mirrors:
  internal:
    ubuntu:
      url: http://your-internal-mirror.local/ubuntu
    debian:
      url: http://your-internal-mirror.local/debian
    rocky:
      url: http://your-internal-mirror.local/rocky
    almalinux:
      url: http://your-internal-mirror.local/almalinux
    centos-stream:
      url: http://your-internal-mirror.local/centos-stream
```

## 支持的存储类型

- 目录类型：dir、nfs、glusterfs
- 块设备类型：zfspool、lvmthin、rbd（厚置备 `lvm` / iSCSI 放不了模板：基卷与链接克隆都不支持，sync 的 `ADD_MISSING` 直接跳过）

## 自定义模板

编辑 `templates.yaml`。顶层 `default_profile` 指定默认导入的配置组，`profiles` 定义配置组（`@minimal` / `@standard` / `@full`），
`image_mirrors` 定义镜像下载备用站；每个模板支持：

- `name`：模板名称（= PVE VM 名；平台按名字认模板，大小写不敏感。系列由名称前缀自动推断：`ubuntu-24.04` → `ubuntu`、`almaLinux-9` → `alma`）
- `family`：可选，显式指定系列名
- `url`：镜像下载地址（配了 `discover` 时作为兜底）
- `discover`：`{index, pattern}`，在目录索引里按正则（整个文件名、捕获组按数字比较）找最新点版本——
  上游改命名（2026-11 CentOS Stream 10、2026-10 Alpine 去掉 `generic_` 前缀）只需改 pattern
- `checksum`：校验文件地址（`{url}` / `{dir}` 占位）；`checksum_pattern`：校验文件名随构建号变化时（Fedora）在目录里找
- `upgrade`：构建时是否做包升级（默认 `true`；Arch 滚动镜像为 `false`）
- `eol`：`true` = 已停止维护，或 `'YYYY-MM-DD'` = 到这天起算（工具按日期判定）；EOL 的不补缺、不按年龄重建，不进 minimal / standard 组。
  不参与配方指纹（改日期不会触发重建）
- `min_cpu_level`：宿主机至少要 x86-64-v<N>（EL10 的 rocky-10 / almaLinux-10 / centos-stream-10 为 3：没有 AVX2 的 CPU 上内核直接 panic）。
  达不到的节点 sync 记 `skipped`、不补缺；旧模板 cpu 不是 `host` / `max` / `x86-64-v3+` 时新模板改用 `host`。不参与配方指纹
- `display_name`：平台显示名（`manifest.py` 输出；默认「系列名 + 版本」，如 `Debian 13`）。不参与配方指纹
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
