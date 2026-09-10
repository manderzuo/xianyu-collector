闲鱼管理系统安装更新包
版本：2026-09-10 Docker/WSL 安装修复

用途
----
修复两类问题：
1. Docker Desktop 已安装但 winget 提示“已安装且无适用升级”，安装器误报 Docker Desktop missing。
2. WSL 更新输出在中文 Windows PowerShell 环境中出现乱码。

使用方法
--------
1. 关闭正在运行的闲鱼管理系统安装窗口。
2. 将本更新包内 resources\docker-bootstrap.ps1 覆盖到旧安装包的 resources\docker-bootstrap.ps1。
3. 将本更新包内 resources\prepare-wsl.ps1 覆盖到旧安装包的 resources\prepare-wsl.ps1。
4. 重新运行旧安装包中的安装程序。

本更新包仅替换两个安装脚本，不包含 Docker 镜像、应用数据、数据库、配置文件或用户数据；不会主动删除 Docker 镜像和卷。

文件校验（SHA-256）
-------------------
resources\docker-bootstrap.ps1
6FA70438F720C73F9B2C33E6641FDF0C1F58361E728F7B486F51E628F8B19EC8

resources\prepare-wsl.ps1
DF43152E314EDC5D29551473FFA4BD69765AE4FBAC546A384C9CB0DAB23B95DC

如果覆盖后仍失败，请保留安装窗口中的完整错误信息，并提供安装目标目录下 app\logs\install.log。
