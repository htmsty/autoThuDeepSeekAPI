# 发行版留档

从 2.9.0 起，这里的主发行包是**多文件 ZIP**
（`autoThuDeepSeekAPI-<版本>-portable.zip`），单文件
`autoThuDeepSeekAPI-<版本>.exe` 只作兼容备用。

**为什么不进 Git**：ZIP 与单文件都约 165 MB，超过普通 Git 文件 100 MB 的限制，
所以这些产物只在本机留档，仓库里只记录校验和。GitHub **Release 资产**的限额更高，
发布时把 ZIP 作为主资产，EXE 作为备用资产上传。

```
releases/
  2.0/
    autoThuDeepSeekAPI-2.0.exe
    SHA256.txt          # 校验和 + 生成时间 + 对应提交
  2.1/
    ...
  2.9.0/
    autoThuDeepSeekAPI-2.9.0-portable.zip
    autoThuDeepSeekAPI-2.9.0.exe
    SHA256.txt
  2.9.1/
    autoThuDeepSeekAPI-2.9.1-portable.zip
    autoThuDeepSeekAPI-2.9.1.exe
    SHA256.txt
```

从 2.7.0 起，打包的 EXE 带文件版本资源；没有 Authenticode 证书时 Windows 仍会显示
“未知发布者”。需要代码签名时必须单独提供证书，不在仓库内保存私钥。

想重新生成某个版本的程序：

```powershell
git checkout v2.0          # 或 v2.1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\build\build_app.ps1
```

完整的 `build_app.ps1` 会在 `dist\` 生成版本化 ZIP、展开目录和单文件版；
只需要单文件排查时可用 `-OneFileOnly`。

## 已知有问题的留档

| 版本 | 问题 |
| --- | --- |
| 2.4 | **设置界面打不开**（点「打开配置」出来空白窗口）。请用 2.4.1 或更新版本。 |
