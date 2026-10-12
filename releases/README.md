# 发行版留档

这里存放每个大版本**打包好的单文件程序**（`autoThuDeepSeekAPI-<版本>.exe`）。

**为什么不进 Git**：单文件版约 180 MB，超过 GitHub 单文件 100 MB 的硬上限，
所以这些 exe 只在本机留档，仓库里只记录校验和。

```
releases/
  2.0/
    autoThuDeepSeekAPI-2.0.exe
    SHA256.txt          # 校验和 + 生成时间 + 对应提交
  2.1/
    ...
```

想重新生成某个版本的程序：

```powershell
git checkout v2.0          # 或 v2.1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\build\build_app.ps1 -OneFileOnly
```
