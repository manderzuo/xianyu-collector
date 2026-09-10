闲鱼管理系统安装更新包
版本：2026-09-10 首次启动更新检查修复

使用方法
--------
1. 关闭闲鱼管理系统和更新窗口。
2. 解压本更新包。
3. 按压缩包内的相对路径，将 4 个脚本覆盖到旧安装包同名位置。
4. 重新打开闲鱼管理系统。

本次包含的文件
--------------
resources\docker-bootstrap.ps1
resources\prepare-wsl.ps1
app\deploy\check-xianyu-update.ps1
app\deploy\update-xianyu-gui.ps1

修复内容
--------
- Docker Desktop 已安装但 winget 提示“已安装且无适用升级”时，不再误报 Docker Desktop missing。
- WSL 中文输出不再因 PowerShell 原生输出编码产生乱码。
- 首次安装或直接打开更新器时，如果 app\.env 尚未生成，跳过本次更新检查，不再弹出退出码 1 和 Docker Compose 环境文件错误。

本更新包只替换安装脚本，不包含 Docker 镜像、应用数据、数据库、配置文件或用户数据；不会主动删除 Docker 镜像和卷。

文件校验（SHA-256）
-------------------
resources\docker-bootstrap.ps1
6FA70438F720C73F9B2C33E6641FDF0C1F58361E728F7B486F51E628F8B19EC8

resources\prepare-wsl.ps1
DF43152E314EDC5D29551473FFA4BD69765AE4FBAC546A384C9CB0DAB23B95DC

app\deploy\check-xianyu-update.ps1
091D30D0925326DCA420350584099413775C625D34109E0D8B3A2BC07CEA34B5

app\deploy\update-xianyu-gui.ps1
FEE8E4F7FF930A4B7B37BE1991398B9C737DEC48D2985168D340D656751F4EDD

如果覆盖后仍失败，请保留安装窗口中的完整错误信息，并提供安装目标目录下 app\logs\install.log、app\logs\startup.log 和 app\logs\update.log。
