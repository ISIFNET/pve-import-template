# Changelog

本文件记录对 pve-import-template 的主要改动。

## [2.3.0] - 2026-11-08

### OpenSSH 10.6p2（musl + 无 PAM 的 sshd 密码认证绕过）

- 新增模板键 `min_packages: {包名: 最低版本}`：构建信息 `/etc/pve-template-release` 写 `MIN_PACKAGE=<包> <版本>`，`verify-image.sh`
  用镜像自己的包管理器核对（apk `version -t` / `dpkg --compare-versions` / rpm lua `vercmp`；Alpine 3.19–edge、Debian 13、Rocky 8 / 9
  容器实测），没装、版本更低、比不了 = 构建失败、旧模板保持在役（镜像站滞后时不换成同样有漏洞的新构建）。进配方指纹：提高下限即全机队重建。
- Alpine 3.22 / 3.23 / 3.24 带上 openssh-server 下限 `10.0_p1-r11` / `10.2_p1-r1` / `10.3_p1-r2`（Alpine 2026-11-05 回移的
  `main/openssh: fix password authentication`）。已有的 Alpine 模板下一轮 sync 按配方变化重建（不等 `SOURCE_DELAY_HOURS`）。

### EOL 模板不再获取

- `templates.yaml` 新增顶层 `retired` 列表：ubuntu-18.04 / 20.04、debian-10 / 11、fedora-42、opensuse-leap-15.6 从 `templates` 挪过去。
  import / sync / 向导 / `--all` / `@full` 都不再列出或构建；节点上已有的同名模板原样保留，`--list` 标 `eol, no longer fetched`
  （JSON `dropped_eol: true`）。`manifest.py` 仍输出它们（`eol: true`、`retired: true`），供平台显示名与 EOL 标记。
- 同一个名字同时在 `templates` 与 `retired` 里、retired 条目名字非法：读配置时报错。`import.py --list` 末尾列出 retired。

### 新版本

- 新增 opensuse-leap-16.1（2026-11-03 发布，支持到 2028-10-31）。Alpine 兜底地址更新到 3.22.8 / 3.23.8 / 3.24.4（discover 照常找最新）。

## [2.2.0] - 2026-11-06

### 回滚、hold、暂停、金丝雀

- `sync-templates.py --rollback NAME`：在本节点 VMID 区间内找最新的、带 `pit-retired`、名字形如 `<NAME>-retired-<UTC>`、未锁的退役模板，
  改回 `<NAME>` 并去掉 `pit-retired`（其它 tag 保留），当前在役的同名模板按标准流程退役（先恢复再退役；退役失败撤销恢复）。
  没有可恢复版本时退出码 1。写 hold：`holds[NAME] = {at, by: "rollback", bad_vmid, bad_source, restored_vmid, bad_url, bad_checksum}`，
  本次运行也写进 `last-run.json`（行 `action=rolled-back`，其它模板的行保留）。`--release NAME` 删除 hold。
- hold：`stale` / `recipe-changed` / `legacy` / 同一个坏源的 `source-changed` / pending forced 全部跳过（`action=held`），`--force` 不越过；
  上游源标识变了（新文件名，或同一地址校验和变了）自动解除，行 detail 注明 `hold released: new upstream image`；目录索引打不开不算变。
- `PAUSED=1`：sync 拿锁后直接结束（退出码 0），状态写 `paused: true` / `message`，退避 / hold / pending / 游标保留。
- `SOURCE_DELAY_HOURS`（默认 24）：上游新镜像首次见到后满 N 小时才采用（`action=waiting`），状态记 `sources_seen`；0 = 金丝雀立即采用；
  `--force` 不受限；只影响 `source-changed`。

### 强制重建不再丢失、资源不够只推迟

- 被 `MAX_BUILDS` / `RUN_BUDGET_HOURS` / 低内存 / 空间不够 / appliance 起不来推迟的、以及被中断的强制重建写进 `pending_forced`，
  之后不带 `--force` 的运行当 `forced` 建（detail `pending forced rebuild since …`，遵守失败退避），建成后清除。
- 新配置 `MIN_FREE_MEM_MB`（默认 4096）：构建前、冒烟前读 MemAvailable，不够 `deferred`（`low memory: …`），不记失败、不进退避。
- 空间保留量：缓存 / 工作目录写入后至少剩 max(10 GiB, 10%)，目标存储至少剩 max(20 GiB, 10%)，lvmthin 的 thin pool 元数据 ≥ 80% 不再写；
  不够 `deferred`（`not enough space: …`）。下载前先预检存储。libguestfs appliance 内存 `LIBGUESTFS_MEMSIZE=1024`。
- `RUN_BUDGET_HOURS`（默认 4，0 = 不限）：本轮超过时长后不再开新构建（`run budget exhausted`）。
- libguestfs appliance 起不来（LaunchError）后，本轮其余模板直接 `deferred`，不再逐个下载、逐个失败。
- 集群没有 quorum：退出码 4，整轮跳过，状态写 `ok=false` + `message`，不记 failures；systemd 单元 `SuccessExitStatus=3 4`。

### 冒烟加深

- `SMOKE_DEEP=1`（默认）：qga 应答后、同一 `SMOKE_TIMEOUT` 预算内，`qm guest exec` 轮询 `cloud-init status` 到 done / disabled
  （degraded 也算过；error 或超时仍 running 失败；来宾没装 cloud-init 跳过），再查 SSH host key 已生成、`sshd -t` 通过、
  sshd 服务或 socket 在跑（Ubuntu 22.10+ 是 socket 激活，`ssh.service` 平时就是 inactive）。agent 不支持 `guest-exec` 只记告警。

### 切换安全

- hookscript 防御检查真正生效：切换前读新模板的**实际**配置，与旧模板的**原始** hookscript 比较，没带上 / 不同就不切换（并删掉这次建的 `-pitbuild`）；
  旧模板 hookscript 格式不认识则根本不建（旧版这个检查和沿用的配置同源，恒为假）。
- 沿用的节点策略键增加 `nameserver` `searchdomain` `cicustom` `ciupgrade` `numa` `tablet` `rng0` `scsihw` `ostype` `args`（逐个正则白名单，
  不经 shell；`protection` 不沿用）；其余旧模板有而新模板没有的键列在行 `warnings` 里。
- EL10（rocky-10 / almaLinux-10 / centos-stream-10）`min_cpu_level: 3`：宿主机达不到 x86-64-v3 记 `skipped`（`host CPU lacks x86-64-v3`）、不补缺；
  旧模板 cpu 不是 `host` / `max` / `x86-64-v3+` 时新模板改用 `host`。`min_cpu_level` / `display_name` 与 `eol` 一样不进配方指纹。
- VMID 游标除了本节点状态，再读写集群共享文件 `/etc/pve/pit-vmid-hw`（取较大者，写失败不致命）；游标记最近分配的号，转满一圈回绕后跟着回到低位。
- 撞号：`already exists` / `File exists` 都换号重试（卷分配阶段撞号也删掉自己重来），存储上已有同号卷的 VMID 直接跳过；冒烟退回全克隆那一步也重试。

### 清理与中断

- 中断时状态文件保留本轮明细（`templates` 直接引用行列表）；在建模板记 failures `kind=interrupted`，退避 2 小时。
- 半成品清理：带 clone / create 锁的 `pitsmoke-*` / `-pitbuild` 核对 tag / 元数据后 `--skiplock` 删，其它锁记 gc 备注；
  构建 / 冒烟失败后用 `pvesm list --vmid` 找残留卷，只删这次才出现的 `vm-<id>-*` / `base-<id>-*`；中断时删 `.img.download`，下一轮清理也清；
  清理命令都有超时。`CACHE_DIR` 可在配置里指定。
- `ADD_MISSING` 遇到放不了模板的存储（厚置备 lvm / iscsi / iscsidirect / pbs / cephfs / esxi、不允许 images）直接 `skipped`，不下载。
- GC 对本节点所有带 `pit-retired` 的 templates.yaml 名字执行（不只本轮 built / current 的行；未选中名字的结果在报告 `gc` 里）。

### 报告与输出

- 状态文件 / `--json` 新增 `paused` `holds` `pending_forced` `sources_seen` `message`；`summary` 含全部 action 的计数（没有的记 0）；
  每行补 `storage` `format` `bridge` `hookscript` `next_vmid` `source`（上游文件名；建成的行另有 `source_url`）`reason`。
- 行 `action` 收敛到固定词表：built / current / failed / deferred / backoff / absent / held / waiting / skipped / would-build /
  would-retire / would-delete / rolled-back。`ADOPT_LEGACY=0` 时的旧模板由 `action=legacy` 改为 `skipped` + `reason=legacy`。
- `--dry-run`：将删除的写成 `would-delete`，`would-retire` 也打印；计划行带 storage / fmt / bridge / hook / vmid≈ / src / reason。
- `--list`：标出区间外（`out-of-range`，不再算 legacy）、是否在 yaml、是否 EOL、当前配置是否维护、存储、有无 hookscript、
  退役多久 / 能否回收、protection、lock。
- 结尾单独的 `--`（`--json --`）统一丢弃，不同 Python 版本行为一致。
- 新增 `manifest.py`：输出模板清单 JSON（`tool_version` / `default_profile` / 展开后的 `profiles` / 每个模板的 family、版本、显示名、
  eol、min_cpu_level），平台 vendoring 时生成 `templates.manifest.json`。

### 其它

- `uploads/upgrade-packages.sh`：去掉 Ubuntu 的 `Always-Include-Phased-Updates`（模板不该比普通机器更早吃到还在分阶段推送的包）；
  dnf / yum / microdnf 与 apt 一样重试。**脚本内容变了 = 配方变了**：升级后各节点会按 `recipe-changed` 把开了包升级的模板重建一遍
  （`MAX_BUILDS` 限流，几天内完成）。
- EL 换源按发行版区分：`mirrors` 新增 `rocky` / `almalinux` / `centos-stream` 条目（Stream 9/10 把 metalink 改写成 baseurl）；
  只配了旧 `rhel` 条目的镜像站不再把 Rocky / Alma / Stream 改到 `…/centos` 路径（会 404），而是保持官方源。huawei 暂未配 EL 条目。
- `update-templates.py --all` 默认跳过带 `pit` tag 的模板（2.x 托管的不能就地改），`--include-managed` 才处理。
- README：回滚 / 暂停 / 金丝雀 / 备份排除模板章节；agent 安装的节点（`/opt/pve-import-template`）没有 run.sh / tui.py 的用法说明；
  minimal 列表补 rocky-9；配置示例补全新键。

## [2.1.0] - 2026-11-06

### 默认导入哪些系统

- 新增 `default_profile`（默认 `standard`），取代按系列的 `default_families`：`import.py` 不指定模板、`tui.py` 向导默认勾选、
  sync 的 `ADD_MISSING`（没写 `PROFILE` / `SELECTOR` 时）都用它。旧版按系列选会把 EOL 的 ubuntu-18.04 / 20.04、debian-10 / 11 一起导进来。
- 配置组按支持期重排：`minimal` 加 rocky-9（小硬盘也有一个 EL）；`standard` 加 rocky-8 / almaLinux-8（安全支持到 2029-05），
  去掉 alpineLinux-3.22（2027-05 EOL）。测试钉死组里没有 EOL 模板。
- `eol` 改为日期（endoflife.date 的安全支持截止日）：到期自动生效，不用再改 yaml；`--list` 显示「支持到」。
  `eol` 不参与配方指纹——改日期不会触发全机队重建。
- Alpine 3.24：上游 3.24.2 起不再分 bios / uefi 变体，discover 两种命名都认（停在 3.24.1 的限制去掉），
  能否 SeaBIOS 启动由切换前的开机冒烟实测；向导多一个「小硬盘 minimal」预设。

## [2.0.0] - 2026-11-05

### 新增 sync-templates.py：模板持续更新（蓝绿重建）

- **新建 → 切换 → 回收**：不再就地改模板基卷（链接克隆共享基卷，就地写要么被拒、要么不进新克隆、要么改坏已有克隆）。
  新模板构建完全成功后才接管名字，旧模板改名 `<name>-retired-<时间>` + tag `pit-retired`，保留 `KEEP_RETIRED` 代，
  更早的在退役满 12 小时、无链接克隆、无 protection、未加锁时删除。
- **重建判断**：forced / source-changed / stale（默认 30 天）/ recipe-changed（yaml 条目 + 注入脚本 + 工具版本指纹）/ legacy / missing；
  `--max-builds` 限流，按优先级排序；失败按配方指数退避（404 固定 7 天，改了 yaml 立即重试）。
- 非交互、`--json`、`--dry-run`、`--list`；状态文件 `/var/lib/pve-import-template/last-run.json`；节点级 flock（三个工具互斥）；
  SIGTERM 走清理；systemd timer（`install-timer.sh` / `bash run.sh timer install`）；节点配置 `/etc/pve-import-template.conf`（严格解析，不经 shell）。
- **profiles**（`@minimal` / `@standard` / `@full`）：小硬盘节点只维护几个主流模板；构建前检查缓存目录与目标存储剩余空间；
  `KEEP_CACHE=0`（默认）构建完删缓存镜像。

### 构建流水线（import.py 与 sync 共用）

- 缓存镜像保持上游原样，定制只在副本上做（旧版直接定制缓存，下次复用时脚本叠加执行、sed 叠两遍）。
- **包升级**（`uploads/upgrade-packages.sh`，apt/dnf/yum/zypper/apk/pacman，失败即构建失败；EOL 只在源确实不可用时切归档源）。
- **注入校验**（`uploads/verify-image.sh`）：qga 已装且自启、`sshd -T` 生效配置 permitrootlogin=yes，不达标构建失败。
- **封装清理**（`uploads/finalize-image.sh`）：SSH host key、machine-id、cloud-init 状态、随机种子、pacman 临时 keyring、日志、包缓存、历史。
- 模板 tags（`pit` / `pit-bYYYYMMDD`）+ description 元数据；镜像内写 `/etc/pve-template-release`。
- `qm set --scsi0 <storage>:0,import-from=…` 一步导入挂盘（旧版写死 `disk-0`，存储上有残留盘时挂错盘）；`--boot order=scsi0`。
- 上游校验和（SHA256/SHA512，GNU / BSD / 单 hash），`discover` 自动找最新点版本，`image_mirrors` 备用下载站；下载有超时、
  只对瞬时故障重试、失败分类；ETag 不同但 Last-Modified 相同不再误判「没变」；缓存元数据记 URL。
- RHEL / Fedora / SUSE 构建加 `--selinux-relabel`；appliance 起不来才回退 TCG（脚本出错不再整套重跑）。
- VMID 分配把 LXC 算进占用；失败的号跳过不再连环失败；`--only-new` 只看本节点；多个选择器合并。

### templates.yaml

- 全部模板配上校验和；CentOS Stream 9/10 改 discover（2026-11 上游改名，旧 10-stream 地址 502）；Alpine 3.22/3.23 discover
  （自动用上 3.22.7 / 3.23.7，2026-10 上游去掉 `generic_` 前缀）；Fedora 43/44/45 discover（新增 45，42 标 eol）；
  Arch `upgrade: false`；EOL 版本标 `eol: true`；新增 `profiles`、`image_mirrors`（TUNA：Ubuntu cloud images / Alpine）。

### 审查加固（同日）

- **切换前开机冒烟**：新模板链接克隆一台不接网卡的临时 VM，`SMOKE_TIMEOUT` 内 qemu-guest-agent 不应答 = 构建失败（防包升级弄坏引导后自动上线）。
- VMID 单调分配（高水位记在状态文件），`qm create` 前一刻才分配、撞号换号；退役时间戳取改名那一刻，宽限期 12h；GC 只认 `pit-retired` + 可解析时间戳，
  不再带 `--destroy-unreferenced-disks`；只管理 VMID 区间内的模板；同名多个在役时补退役；改名撞克隆锁退避重试。
- discover 只往上走、同版本按文件名定序；决策用的 URL 直接交给构建；有校验文件时按上游 hash 判断是否重新发布。
- 镜像 `qemu-img info` 核对格式 / backing file / data-file，virt-customize 显式 `--format`，单镜像 8 GiB 上限，TCG 重试从干净副本开始。
- 选择器不得以 `-` 开头、argparse 不认缩写（防止拼出 `--insecure` 之类的选项）；配置非法 / 缺依赖也写状态文件。
- openSUSE Leap 16（主配置在 `/usr/etc/ssh`）写 drop-in；deb822 归档切换只删 `*-updates` 套件；verify 增加 cloud-init 检查；被强杀的半成品 / 冒烟 VM / 工作副本自动清理。

### 修复

- Debian `--mirror` 拼出 `…/debian/debian`（换源后 apt 全挂）；Fedora 不再套 RHEL 换源规则；RHEL 换源认 `metalink` 与 `# baseurl`。
- `install-qga.sh` 不再对所有 Debian 永久关闭 Valid-Until、不再把在支持期的 Ubuntu 改到 old-releases。
- `permit-root-login.sh` 改 drop-in / 首行写入，去掉行尾注释。
- `update-templates.py`：默认网络调优尊重 yaml 的 `net_tuning: false`（Alpine）；失败日志改到 0700 目录、O_EXCL 打开
  （旧版写 `/tmp` 下可预测路径）；与其它工具共用节点锁。
- 默认起始 VMID 9000 → 9001（rhapsi 平台只识别 9001-9999）。
- 新增 `tests/`（stdlib unittest：`python3 -m unittest discover -s tests`）。

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
