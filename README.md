# pve-import-template

New

Wget
```
wget -O - https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```

```
curl https://raw.githubusercontent.com/ISIFNET/pve-import-template/refs/heads/master/run.sh | bash
```


安装 git 并克隆仓库：

```
apt install -y git
git clone https://github.com/balthild/pve-import-template
cd pve-import-template
```

添加 no-subscription 源并安装所需依赖：

```
./setup.sh
```

下载并导入镜像，以 local-lvm 存储为例：

```
./import.py local-lvm
```
