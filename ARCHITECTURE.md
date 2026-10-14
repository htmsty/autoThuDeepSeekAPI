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

private 的位置**可以改**（2.5 起）：

- 解析顺序：位置指针 → 程序目录下的 `private/` → `%LOCALAPPDATA%` → 临时目录兜底。
- 指针文件：`%LOCALAPPDATA%\autoThuDeepSeekAPI\private-location.json`，只记 `path` 与
  `migrated_from`。放在 `LOCALAPPDATA` 的原因：不能放在 private 里（换了位置就找不回来），
  也尽量不在桌面留文件；「干净卸载」会删掉它。
- `validate_private_dir()` 拒绝系统目录（Windows / System32）、Program Files、ProgramData、
  盘符根、临时目录、桌面，并要求可写。
- `switch_private_root()`：把 `MANAGED_ENTRIES` 复制到新位置 → 写指针 → 尽力清掉旧位置，
  用户自己的文件不动、目录不整个删；`finish_pending_migration()` 在下次启动时补删残留。
- `MANAGED_ENTRIES` 是"程序自己的东西"的唯一定义：设置、状态、模型缓存、锁、日志、备份、
  浏览器会话。换位置与卸载都按它来。
- **删除一律先走回收站**：`recycle_path()`（Windows Shell `SHFileOperation` + `FOF_ALLOWUNDO`）
  优先，回收站不可用时才永久删除（`delete_path()` 会说明用的是哪种）。
  旧目录清空到只剩程序自己时，连目录一起删；还剩用户文件就保留。
- `defer_creation()`：启动早期只判断不落盘，避免"桌面提醒还没弹，private 已经在桌面上"。
  `finish_pending_migration()` 只有在旧位置真的清空了才清掉 `migrated_from`，
  否则留着重试标记，下次启动继续清。

### `instance.py`
单实例锁。`acquire_with_retry()` 给"旧进程正在退出"留时间（默认 6 秒），
`release()` 供"重启程序 / 干净卸载"前主动让出——不然新进程会被判成"程序已经在运行"而退出。

### `data_location.py`
private 位置与桌面提醒的共用流程（启动早期主窗口还不存在时也要用）：
`ask_private_dir()` / `confirm_move()` / `move_private_dir()` / `choose_private_dir()` /
`desktop_notice()`。三个按钮是「选择 private 位置…」「仍然继续」「退出程序去换本程序位置」。

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
  认领规则（2.5.6 起**只认两个信号**，其它一律不碰）：
  1. `baseURL` 的域名就是 `madmodel.cs.tsinghua.edu.cn`（认得出就是我家的服务）；
  2. provider 键名就叫 `madmodel`，且它确实像我们的（站点地址 / 我们的 `apiKeyEnv` /
     键名为 madmodel 的本地转发）。
  **"光有 `apiKeyEnv: MADMODEL_API_KEY`"不再作为认领依据**：老版本曾用"本地转发就算我们的"
  这条宽规则误改过 `ollama` / `isearcher` 这类别人的本地 provider，并把我们的 `apiKeyEnv`
  写在它们身上；再用这个标记去认领，就会一次又一次继续改人家（用户报的
  `isearcher` + `infini-ai` + `does not support reasoning effort "low"` 就是这么来的）。
  写之前还有一道硬护栏：如果该 provider 的 `apiKeyEnv` 另有其人且域名不是清华站，直接报错退出、
  一个字节都不写。扫到"带着我们的标记、地址却不是清华站、键名也不是 madmodel"的 provider 时，
  只提醒用户去检查/从备份还原，程序不动它。
  不勾"统一所有 profile"时，profile 是否归我们管只看正文里有没有清华域名或 `madmodel:` 键
  （不再看"出现过 MADMODEL_API_KEY"，避免被残留标记带偏）。找不到我们的 provider 时，
  `madmodel` 键空着才新建；键被别的配置占用则**报错并保持文件原样，不覆盖**。
- 只合并我们负责的字段：`apiKeyEnv` 与 `models`；`displayName` 只在空、或被填成模型 id 时纠正，
  `baseURL` / `api` / `reasoning` 只在缺失时补上，用户自己改过的值不动。
- **思考档（2.5.3 起的规则）**：provider 级 `reasoning` **只在"用户设的值有模型不支持"时**才改成
  公共档位（否则 DSH 会直接报 `UNSUPPORTED_REASONING_EFFORT`）；合法的用户值一个字都不改，
  原本没有这个字段也不替用户加（只有程序新建 provider 时才写一个公共兜底档）。
  `agent-default-model.reasoningEffort` 同理：合法保留、非法或缺失才补。
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
- 刷新"同站点旧卡片"的令牌时只替换**值是 JWT**、或字段名**正好**是
  `OPENAI_API_KEY` / `api_key` / `token` 之类的位置（2.5.5 起），
  不再按"名字里含 token 就算"的宽规则乱换 —— 同一张卡片里别的模型的 key 必须原样保留。

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

### `uninstall.py`
「干净卸载」：可选先还原 DSH / CC Switch 到第一次备份，再按 `paths.MANAGED_ENTRIES`
清掉程序自己的运行数据与位置指针。**不删程序自身**（界面提示用户手动删），
只用正常的文件操作，不写自删除脚本、不调用 `rd` / `del` / `reg`。

### `procinfo.py`
查当前在跑的进程名（`tasklist`，查一次缓存一次）。只用于"某个程序到底装没装"这类判断：
装了但还没生成配置文件时，进程名是一条有用的线索。

### `logging_setup.py`
日志放 `private/logs/`，按大小轮转（2 MB × 5）。有一个坑要记住：
**重新配置日志时必须真的 `close()` 掉旧的文件 handler** —— 只从 logger 上
`removeHandler()` 并不会释放文件句柄，Windows 上那个日志文件就删不掉、也搬不走
（2.5.3 修的 WinError 32 就是这个）。模块里另外记了一份"开过的文件 handler"清单，
`close_file_handlers()` 把当前挂着的与历史遗留的一起关掉；换 private 位置与干净卸载
之前都会先调它。

### `startup.py`
开机启动（HKCU 的 Run 项）与桌面快捷方式。启动时会核对注册表实际值：路径变了自动纠正，
值为空（被别处写坏）直接重写。`create_desktop_shortcut()` 用系统自带的
PowerShell + `WScript.Shell` 生成 `.lnk`，路径通过环境变量传入以免中文/空格被命令行弄坏，
并隐藏子进程窗口。`is_app_on_desktop()` 判断程序是不是**直接**在桌面根目录（子文件夹不算），
`launch_command()` 供"重启程序"用。

### `state_store.py`
`state.json`（在 private 里）：最近 Token、时间戳、上次同步摘要，以及两条启动提醒的
"已处理"标记（`desktop_note_ack` / `targets_note_ack`）——按需求写在 private，
所以它们也会随"换位置"一起搬走、被"干净卸载"一起清掉。

### `sync.py`
编排：校验 Token → 逐个目标探测 → 备份 → 写入 → 汇总报告。不依赖 Qt，可单独测试。

界面侧：`start_sync()` 有忙判断（正在同步或正在取 Token 时忽略新请求），并且在
"CC Switch 正在运行"对话框关闭后会**再查一次**，避免等待对话框期间自动同步已经跑起来、
又被点一次按钮导致两轮同步并行。

2.5 起还负责两件事：

- **安全终止**：`run_sync(..., cancel_event=...)` 在步骤之间检查取消信号（校验前后、
  每个目标之前），被取消时如实标记 `report.cancelled` 并停止，不会留下写了一半的文件。
  界面上的「强行终止并重试」走的就是这条路，**从不**调用 `QThread.terminate()`。
- **启动提醒的判据**：`target_traces()` / `both_targets_missing()` 判断 DSH 与 CC Switch
  是不是"一点痕迹都没有"（配置目录、进程、常见安装目录任一命中就算有），
  只有真的两边都没有才提醒"先安装"。

### `ui/`
- `main_window.py`：状态区 + 操作按钮（含「创建桌面快捷方式」）+ 设置项 + 日志视图 + 托盘。
  托盘图标左键开窗、右键菜单；关闭窗口即隐藏到托盘。
  启动流程（2.5 起）：`startup_then_sync()` → 先 `_check_desktop_private()`（程序在桌面根目录
  时提醒一次，三个选择），再 `_check_targets_missing()`（两边都没痕迹时提醒一次并跳过这次同步），
  都通过才跑"启动时立即同步"。两条提醒的"已处理"状态写在 private 的 `state.json` 里。
  「强行终止并重试」也在这里：`_ask_force_retry()` + `_retry_after_stop`。
  2.5.1 起，桌面提醒其实由 `app.py` 在**创建任何目录之前**调 `data_location.desktop_notice()`
  完成（`MainWindow` 里那条只是兜底）；搬家/卸载前会先 `_prepare_for_data_move()` /
  `prepare_for_uninstall()` 放开浏览器档案、日志句柄与单实例锁，否则那些文件删不掉。
- `settings_dialog.py`：**只有一个挂在对话框上的布局**（滚动区 + 底部按钮）。
  千万不要再造第二个 `QVBoxLayout(self)`：后建的会把先挂的布局顶掉，对话框会变成空窗口
  （2.4 踩过一次，`test_settings_dialog_is_actually_openable` 是这条的回归用例）。
  2.5 起多了「运行数据与维护」一块：private 位置（+ 更改位置）、恢复默认设置、干净卸载。
- `workers.py`：把同步放到 `QThread`，避免界面卡死。
  `request_cancel()` 只是给同步流程一个"请停下"的信号（协作式取消，见 `sync.py`）。

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

## 写入面清单（"绝不动用户其它配置"的落地方式）

用户明确要求：任何交互都不能改动**别人的**配置。下面是我们会写的每一个文件、写的范围，
以及保证方式（2.5.7 / 2.5.9 起；每一条都有对应用例）。

| 文件 | 只写什么 | 保证方式 |
| --- | --- | --- |
| `.dsh/.credentials.yaml` | `refs.MADMODEL_API_KEY` 这一行 | **行级替换**：别的地方（其它工具的 key、`records`、注释、单引号风格）一个字节都不动；只有文件坏掉需要重建时才整份重写（先留底、日志说明） |
| `.dsh/profiles/*/cordis.patch.yml` | ① 我们自己的 provider 块（`apiKeyEnv` / `models` / 缺失字段）；② 真要改时的 `agent-default-model` 条目 | **块级替换**：把新文本里这两块贴回原文件，其余部分（别人的 provider、其它条目、注释、引号、流式列表写法）原文照抄；**新增 provider 时只往我们这个条目（`llm-pi-ai`）的 `providers:` 块末尾插入**（同一文件别的条目也可能有 providers，插入范围限定在本条目内）；同一文件里还有别的 provider 时**禁止整份重写**，块/条目定位不到就报错、这次不写；条目定位按"整块文本匹配 id/name"（引号、字段顺序都能认），数据层说条目已存在而文本层定位不到时绝不追加；认领只认"域名是清华站点"或"键名就叫 madmodel"，不再靠 `apiKeyEnv` 这种可能被旧版本污染的标记；写前还有"不是我们的就报错退出"的硬护栏 |
| `.cc-switch/cc-switch.db` | `(deepseek-thu, codex)` 这张卡片的 token / meta / name / is_current | 其它卡片的内容不动；`is_current` 的置位只作用于 codex 应用（"当前供应商"这个语义本身）；**默认不再刷新别人的卡片**（想刷要在设置里显式打开 `顺带刷新同站点旧卡片的令牌`，且只替换 Token 字段） |
| `.cc-switch/settings.json` | `currentProviderCodex` 这一行 | **行级替换** + 替换后 `json.loads` 校验等于目标数据；值没变就连排版都不写；键还不存在时**只往对象末尾最小插入一行**（别处字节不动），同名键不唯一 / 结构不认识才退回整份重排（仍只改值） |
| `.cc-switch/live-state.json` | `apps.codex.proxy_route` 这一行 | 同上；缺 `apps.codex` 这类中间层时整块补出缺的部分，别的应用（例如 claude）的紧凑写法一字不动（`contract` 等其它字段保持原样） |
| `private/**` | 我们自己的设置 / 状态 / 日志 / 备份 / 浏览器会话 / 模型缓存 | 都是本程序的数据，换位置与卸载只按 `paths.MANAGED_ENTRIES` 处理 |
| 桌面快捷方式、开机启动注册表项 | 只在用户点按钮 / 勾选时写 | 不做后台自动写入 |

另外：

- 「一键还原」/「干净卸载」会写回 DSH / CC Switch 的文件 —— 那是用户**明确点的操作**，
  界面上有二次确认与风险说明。
- 写入前一律先备份；写失败有回滚；写坏的目标不会被留在那儿。

