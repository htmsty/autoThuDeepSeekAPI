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
备份保留份数、静默取 Token 超时、日志级别、关窗行为、预检开关、DSH 默认模型。

设置文件带 `settings_version`，读到旧版本时**一次性**迁移并写回（`AppSettings.load`）：

| 版本 | 迁移内容 |
| --- | --- |
| v2 | 「统一所有 profile 到 madmodel」默认勾选 |
| v3 | 默认开启自动同步、间隔默认 180 分钟、默认不弹「CC Switch 正在运行」提醒 |

迁移只发生一次：之后用户手动取消勾选、自己填的间隔都不会被默认值打回。

### `token/`
- `jwt_utils.py`：不验签解码 JWT，取 `exp` 算剩余时间。
  界面显示用 `describe_expiry()`：JWT 自带 `exp` 就用它，没有就按"取回时刻 + 5 小时"
  给出到期时刻（`TOKEN_LIFETIME_HOURS`）。`is_expired()` 返回 `None` 表示"看不出来"，
  这种情况按"仍可用、交给站点校验"处理，不能当成已过期。
- `capture.py`：QtWebEngine 内置窗口。页面脚本在 `DocumentCreation` 注入，
  只做三件事：扫 `localStorage` / `sessionStorage`、钩 `fetch` 与 `XHR` 的响应体、
  扫一次页面文本；命中 JWT 即写入 `window.__atdToken`，Python 侧轮询取走。
  **不做任何模拟点击。** 拿到 Token 后窗口自动关闭；静默续期时不显示窗口。
- `verifier.py`：先用站点的会话接口 `GET /model-api/auth-login`（带 `Authorization: Bearer`）
  判断 Token —— 快、不消耗模型额度；401/403 或 `success:false` 判无效，200+`success:true` 判有效。
  只有它给不出结论（非 200、不是预期 JSON）时才兜底发一次最小对话请求；连不上站点就直接
  记"未知"，不再白等一次模型请求。`fetch_models()` 尝试读模型列表，读不到就回退内置名单。
- `site_models.py`：**模型列表跟着站点走**。站点没有模型列表接口（`/v1/models` 会落回 SPA），
  于是按 thu-tok-auto 的做法读站点自己的前端包：首页 → `assets/js/*.js` → 解析
  `modelList:[{label,value,supportImage,effortOptions}…]` → 转成 provider 的 models 形状
  （`contextWindow` 统一 1000000；排除 `DeepSeek-V4-Flash-Vision-Exp` 与 `DeepSeek-R1-W8A8`；
  思考档只保留 pi-ai 认得的等级）。结果缓存 6 小时，读不到才退回 `/v1/models` 与内置名单。

### `targets/dsh.py`
- 凭据：`<.dsh>/.credentials.yaml` 的 `refs.MADMODEL_API_KEY`。用 ruamel.yaml 往返模式读写，
  保留注释与其它键；写入后重新解析校验值一致。
- 凭据文件已经坏掉（解析失败）时：先整份留底（标签 `dsh-credentials-broken`），再按行捞回
  `大写键: 值` 形式的引用（`DEEPSEEK_API_KEY` 这类；不会把 `kind`/`secret`/`id` 混进去），
  重写成 `version: 1` + `refs` 的合法结构；一个键都捞不回来就如实报错，不改文件。
- 供应商：`<.dsh>/profiles/<profile>/cordis.patch.yml` 里 `llm-pi-ai` 的 `config.providers`。
  先**按标记认领**已有 provider：`apiKeyEnv: MADMODEL_API_KEY` 优先，其次 `baseURL` 指向
  `madmodel.cs.tsinghua.edu.cn`，再次本地转发 `http://127.0.0.1:xxxx/v1`（DSH 里常见的接法）——
  认出谁就更新谁，不会在旁边再建一个重复的。找不到时，`madmodel` 键空着才新建；
  键被别的配置占用则**报错并保持文件原样，不覆盖**。
- 只合并我们负责的字段：`apiKeyEnv` 与 `models`；`displayName` 只在空、或被填成模型 id 时纠正，
  `baseURL` / `api` / `reasoning` 只在缺失时补上，用户自己改过的值不动。
- 模型 id 被写串（例如显示名被填进 id、或默认模型指向别的 provider 的模型）时自动修回；
  勾选"统一所有 profile 到 madmodel"后处理全部 profile，并把 `agent-default-model` 指到
  `madmodel`，同时保证它的 `model` 是 madmodel 下真实存在的 id。

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
- 先算清楚"要写什么"：卡片、`is_current`、同站点旧卡片都无需改动时**不备份也不写库**
  （CC Switch 正在运行时尤其重要）。

### `backup.py`
每次写入前把原文件复制到 `private/backups/<时间戳>-<标签>/`，并写入
`private/backups/index.json`（源路径、备份路径、时间、sha256、是否原始备份）。SQLite 用
`Connection.backup()` 保证一致性。"还原"按钮恢复每个源文件**最早**的那份备份
（即首次修改前的状态），还原前会先把当前状态也备份一份。

- `begin_snapshot(label)`：让同一次操作的多份文件共用一个快照目录。
- `_snapshot_target()`：同一目录里重名（多个 profile 都叫 `cordis.patch.yml`）时自动
  加 `-2`、`-3` 序号，避免互相覆盖导致还原错位。
- `prune()`：每个源文件**最早那份永远保留**（无论有没有 `original` 标记），其余按
  `backup_keep` 轮换。

### `backup_ops.py`
给界面用的封装：手动备份、一键还原、以及 `auto_backup_on_first_run()`——
程序第一次运行时给每个目标文件留一份带 `original` 标记的原始备份，重复调用不会重复记账。

### `startup.py`
开机启动（HKCU 的 Run 项）与桌面快捷方式。启动时会核对注册表实际值：路径变了自动纠正，
值为空（被别处写坏）直接重写。`create_desktop_shortcut()` 用系统自带的
PowerShell + `WScript.Shell` 生成 `.lnk`，路径通过环境变量传入以免中文/空格被命令行弄坏，
并隐藏子进程窗口。

### `sync.py`
编排：校验 Token → 逐个目标探测 → 备份 → 写入 → 汇总报告。不依赖 Qt，可单独测试。

界面侧：`start_sync()` 有忙判断（正在同步或正在取 Token 时忽略新请求），并且在
"CC Switch 正在运行"对话框关闭后会**再查一次**，避免等待对话框期间自动同步已经跑起来、
又被点一次按钮导致两轮同步并行。

### `ui/`
- `main_window.py`：状态区 + 操作按钮（含「创建桌面快捷方式」）+ 设置项 + 日志视图 + 托盘。
  托盘图标左键开窗、右键菜单；关闭窗口即隐藏到托盘。
- `settings_dialog.py`：**只有一个挂在对话框上的布局**（滚动区 + 底部按钮）。
  千万不要再造第二个 `QVBoxLayout(self)`：后建的会把先挂的布局顶掉，对话框会变成空窗口
  （2.4 踩过一次，`test_settings_dialog_is_actually_openable` 是这条的回归用例）。
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

打包注意：`build/atd.spec` 会过滤两类 DLL —— Codex 运行库自带的旧 `api-ms-win-*`
转发 DLL，以及外部工具链（Poppler）里的 ICU。它们会盖住 Qt 需要的系统版本，
导致打包后导入 QtCore 时报 WinError 127。`--selftest` 用来确认打包版能真的抓到 Token。

## 约定

- 只在 `private/` 下写运行数据；仓库内不出现任何 Token、路径或账号。
- 所有写入目标都先备份、再写、再校验；失败时保留现场并说明原因。
- 目标路径全部"默认探测 + 允许手工覆盖"，不写死用户名或盘符。

