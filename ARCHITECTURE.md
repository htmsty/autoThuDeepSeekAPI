# 程序架构与每步要做什么

本文档记录 `autoThuDeepSeekAPI` 的结构，以及每一部分负责的事。修改代码前请先读这里。

## 总览

```
main 启动
  └── app.py 组装：设置、日志、主窗口、托盘、定时器
        ├── token/         拿 Token（内置浏览器 + 校验）
        ├── targets/       把 Token 写进 DSH / CC Switch
        ├── backup.py      写入前备份、一键还原
        └── ui/            主界面、托盘、设置对话框
```

数据流：

```
用户点「立即获取并同步」
  → TokenCaptureWindow 打开内置浏览器（站点登录页）
  → 页面脚本捕获 Token → 自动关窗
  → verifier 校验 Token（向站点发一次最小请求）
  → SyncOrchestrator：
       逐个目标 detect() → backup() → apply()
  → 写入结果汇总 → 界面状态 / 托盘通知 / 日志
```

## 目录

| 路径 | 作用 |
| --- | --- |
| `src/auto_thu_deepseek_api/` | 程序源码 |
| `src/auto_thu_deepseek_api/token/` | 内置浏览器取 Token、JWT 解析、服务端校验 |
| `src/auto_thu_deepseek_api/targets/` | DSH、CC Switch 的探测与写入 |
| `src/auto_thu_deepseek_api/ui/` | 主窗口、托盘、设置对话框、工作线程 |
| `assets/` | 自绘图标与应用资源 |
| `build/` | 打包脚本 |
| `private/` | 运行数据（`settings.json`、`state.json`、`logs/`、`backups/`、`browser-profile/`），不提交 |
| `docs/` | 额外文档 |

## 关键模块

### `paths.py`
程序目录、`private/` 根目录、各默认目标路径与探测辅助。程序目录不可写时自动退回
`%LOCALAPPDATA%\autoThuDeepSeekAPI`。**不写死任何机器相关的位置。**

### `settings.py`
用户设置（JSON）：同步间隔、自动同步、开机启动、启动即同步、同步后最小化、
DSH / CC Switch 各自开关、自定义路径、统一 profile 选项、CC Switch 运行中策略与提示开关、
备份保留份数。

### `token/`
- `jwt_utils.py`：不验签解码 JWT，取 `exp` 算剩余时间。
- `capture.py`：QtWebEngine 内置窗口。页面脚本在 `DocumentCreation` 注入，
  只做三件事：扫 `localStorage` / `sessionStorage`、钩 `fetch` 与 `XHR` 的响应体、
  扫一次页面文本；命中 JWT 即写入 `window.__atdToken`，Python 侧轮询取走。
  **不做任何模拟点击。** 拿到 Token 后窗口自动关闭；静默续期时不显示窗口。
- `verifier.py`：向 `{base}/v1/chat/completions` 发一次最小请求判断 Token 是否可用
  （401/403 判无效，200 判有效，其余判"未知"而不是失败）；`fetch_models()` 尝试读模型列表。

### `targets/dsh.py`
- 凭据：`<.dsh>/.credentials.yaml` 的 `refs.MADMODEL_API_KEY`。用 ruamel.yaml 往返模式读写，
  保留注释与其它键；写入后重新解析校验值一致。
- 供应商：`<.dsh>/profiles/<profile>/cordis.patch.yml` 里 `llm-pi-ai` 的
  `config.providers.madmodel`。默认只处理**已配置过 madmodel 的 profile**；
  勾选"统一所有 profile 到 madmodel"后处理全部 profile，并把 `agent-default-model`
  指向 `madmodel`。

### `targets/ccswitch.py`
- 数据库 `cc-switch.db`：
  - `providers` 行 `(id=deepseek-thu, app_type=codex)`：更新 `settings_config.auth.OPENAI_API_KEY`，
    保留 `config`（Codex TOML）与其它字段；
  - `providers.meta`：写入 `apiFormat = "openai_chat"`（本地路由据此把 Responses 转成 Chat）；
  - `is_current = 1`，并把其它 codex 供应商置 0；
  - 刷新**同站点旧卡片**（`settings_config` 里出现 madmodel 域名的其它供应商）的 Token。
- `settings.json`：`currentProviderCodex = "deepseek-thu"`。
- `live-state.json`：`apps.codex.proxy_route = "deepseek-thu"`（`mode` 保持原样）。
- 写库前用 SQLite 备份接口做一致性备份；`database is locked` 时提示关闭 CC Switch。

### `backup.py`
每次写入前把原文件复制到 `private/backups/<时间戳>-<标签>/`，并写入
`private/backups/index.json`（源路径、备份路径、时间、sha256）。SQLite 用
`Connection.backup()` 保证一致性。"还原"按钮恢复每个源文件**最早**的那份备份
（即首次修改前的状态），还原前会先把当前状态也备份一份。

### `sync.py`
编排：校验 Token → 逐个目标探测 → 备份 → 写入 → 汇总报告。不依赖 Qt，可单独测试。

### `ui/`
- `main_window.py`：状态区 + 操作按钮 + 设置项 + 日志视图。
- `tray.py`：托盘图标与菜单；左键开窗、右键菜单；关闭窗口即隐藏到托盘。
- `settings_dialog.py`：路径、开关、CC Switch 策略、备份还原、手动粘贴 Token。
- `workers.py`：把同步放到 `QThread`，避免界面卡死。

## 每一步要做什么（里程碑）

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M0 | 仓库骨架、README、.gitignore、架构文档、图标 | `git status` 干净可用，文档齐全 |
| M1 | 主窗口 + 托盘 + 设置 + 日志 | 双击可开，`×` 进托盘，托盘左右键行为正确 |
| M2 | Token 获取 | 内置窗口登录后自动捕获并关窗；静默续期可用 |
| M3 | 写入器 | DSH、CC Switch 写入正确且不动无关内容 |
| M4 | 备份与还原 | 首次修改前有备份，"还原"能回到初始状态 |
| M5 | 自动化 | 自动同步、开机启动、启动即同步、同步后最小化 |
| M6 | 打包 | 文件夹版与单文件版均可双击运行 |
| M7 | 实测与发布 | 本机端到端验证通过并推送 GitHub |

## 约定

- 只在 `private/` 下写运行数据；仓库内不出现任何 Token、路径或账号。
- 所有写入目标都先备份、再写、再校验；失败时保留现场并说明原因。
- 目标路径全部"默认探测 + 允许手工覆盖"，不写死用户名或盘符。

