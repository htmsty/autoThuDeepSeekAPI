# 发行版留档

这里存放每个大版本**打包好的单文件程序**（`autoThuDeepSeekAPI-<版本>.exe`）。

**为什么不进 Git**：单文件版约 165 MB，超过普通 Git 文件 100 MB 的限制，
所以这些 exe 只在本机留档，仓库里只记录校验和。GitHub **Release 资产**的限额更高，
发布时可以把 EXE 直接作为 release asset 上传。

```
releases/
  2.0/
    autoThuDeepSeekAPI-2.0.exe
    SHA256.txt          # 校验和 + 生成时间 + 对应提交
  2.1/
    ...
```

从 2.7.0 起，打包的 EXE 带文件版本资源；没有 Authenticode 证书时 Windows 仍会显示
“未知发布者”。需要代码签名时必须单独提供证书，不在仓库内保存私钥。

想重新生成某个版本的程序：

```powershell
git checkout v2.0          # 或 v2.1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\build\build_app.ps1 -OneFileOnly
```

## 已知有问题的留档

| 版本 | 问题 |
| --- | --- |
| 2.4 | **设置界面打不开**（点「打开配置」出来空白窗口）。请用 2.4.1 或更新版本。 |
