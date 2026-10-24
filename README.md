# autoThuDeepSeekAPI

自动获取清华大学 MadModel 平台（<https://madmodel.cs.tsinghua.edu.cn/>）的 API Token，
并写入 DeepSeek Harness（DSH）与 CC Switch 的配置。Windows 便携程序，双击即用。

**?!无限Token?!**

**缺点    tok输出较慢,因此更适合长时间挂着做任务**



- 作者：htmsty
- 许可证：AGPL-3.0（见 [LICENSE](LICENSE)）
- 面向使用者的完整说明：[docs/USER-GUIDE.md](docs/USER-GUIDE.md)（每个按钮、每个设置项、常见问题）

## 它做什么

1. 在程序**内置的浏览器窗口**里打开清华统一身份认证。默认由你自己完成登录；**也可以在设置里
   显式启用 Windows Credential Manager 自动登录**。保存凭据后先在后台尝试登录，成功后不弹出
   浏览器窗口；遇到验证码、二次验证或账号密码错误时才显示窗口并提醒你接手；
2. 登录完成后程序自动捕获 Token、校验有效性，并**自动关闭**该窗口；
3. 把 Token 写入你启用的目标：
   - DSH 凭据 `%USERPROFILE%\.dsh\.credentials.yaml` 的 `MADMODEL_API_KEY`；
   - DSH 供应商配置里的 `llm-pi-ai.providers.madmodel`（名称 `DeepSeek (THU)`，地址
     `https://madmodel.cs.tsinghua.edu.cn/v1`）。只认键名正好是 `madmodel` 且标记属于本程序的
     provider；找不到时只在一个默认托管 profile 中自动创建，不再批量写所有 profile。
     同一文件里**别人**的 provider 即使地址写歪，也只提醒、一个字节都不动；
   - CC Switch 数据库 `%USERPROFILE%\.cc-switch\cc-switch.db` 的供应商卡片
     `deepseek-thu / DeepSeek (THU)`；
   - 只有显式打开「设为当前供应商和代理路由」时，才修改 `settings.json`、`live-state.json`
     以及同 app_type 的 `is_current`。该选项默认关闭。
4. 默认每 60 分钟自动同步一次，每次在 45～75 分钟之间随机；“暂停/继续”可从托盘直接操作。
5. 写入前自动备份。Token、备份和字段变更日志使用 Windows DPAPI 加密；一键还原优先做字段级撤销，
   不会整文件覆盖你后来的其它修改。
6. 资源长期占用有上限：取 Token 完成后会释放内置浏览器的页面与 Chromium 进程，浏览器 HTTP
   缓存最多 32 MiB；日志按 2 MiB × 3 轮转并默认只保留 7 天。登录 Cookie 仍保存在浏览器档案里，
   不会因为释放内存而退出登录。

## 首次运行的自动备份

程序第一次运行时，会先把 DSH 凭据、各 profile、CC Switch 数据库与路由文件各备份一份，
这份**原始备份**带着 `original` 标记，**不会**随备份次数增多而被清理掉；之后每次写入前
的普通备份才按"备份保留份数"轮换。备份文件本身由 Windows DPAPI 加密，复制到别的 Windows
账户后无法解密。

新版还原在设置里点「还原到首次修改前（按字段）」：程序依据加密变更日志，只撤销自己管理、
且当前值仍等于本程序最后写入值的字段。你后来新增或修改的其它字段会保留并报告冲突。
没有字段日志的旧版本备份，只能在额外高风险提示后执行整文件还原。

## 界面与托盘

- 主界面：状态、下次自动同步时间、操作按钮，以及底部可配置的运行日志区。
- 设置分为「同步与启动 / 写入目标 / 登录与 Token / 备份、日志与维护 / 应用与工具」五页，底部固定为
  「应用 / 确定 / 取消」。应用保存但不关闭，确定保存并关闭，取消丢弃尚未应用的修改。
- 主界面日志支持「完整 / 仅同步结果与时间 / 关闭」三档；预检、手动备份、按字段还原、
  数据目录、快捷方式和关于都在对应设置页。文件日志默认保留 7 天，可在设置里改为 1–90 天。
- 托盘菜单固定为：打开主界面、立即获取并同步、暂停/继续自动同步、设置、退出。
- 第一次运行、还没有任何缓存 Token 时，"启动时立即同步"会直接打开浏览器窗口让你登录，
  不再先静默等一轮超时；之后有缓存就静默取。
- 开机启动（`--minimized`）时，第一次同步会先等 90 秒再开始（刚开机网络与磁盘可能还没就绪）；
  自动同步失败后每 5 分钟重试一次、最多 3 次（成功一次就把次数补满）。
  如果静默取 Token 失败（多半是登录会话过期），程序会自动打开登录窗口并在托盘提示。
- 点击 `×` 最小化到系统托盘并从任务栏消失；托盘图标左键单击打开主界面，右键单击弹出菜单。

### 设置里的主要入口

| 入口 | 作用 |
| --- | --- |
| 立即同步 | 打开内置浏览器窗口；已有会话或已保存自动登录凭据时先在后台尝试，拿到 Token 后校验并写入启用的目标 |
| 校验写入内容 | **只读**预检：使用设置窗口里的当前值逐项校验，不动任何文件；按钮会显示进度 |
| 立即备份 | 立刻给 DSH / CC Switch 的关键文件各留一份加密备份 |
| 还原到首次修改前 | 优先按字段撤销；没有字段日志的旧备份需额外确认高风险整文件还原 |
| 打开日志目录 / 打开数据目录 | 打开日志目录、`private/` 数据目录 |
| 更改 private 位置 | 把运行数据搬到别的文件夹（会校验并避开系统目录、临时目录、桌面等） |
| 恢复默认设置 | 恢复默认值；开机启动项与"上次同步"记录不动 |
| 干净卸载 | 可选按字段/旧备份还原 DSH / CC Switch，然后清掉程序自己的运行数据 |
| 创建桌面快捷方式 / 关于 | 创建快捷方式；查看版本、作者、主页和数据目录 |

### 写入前预检

同步前会先把"将要写出去的内容"干跑渲染一遍（和真正写盘用的是同一套代码，只是不备份不落盘），
再校验：YAML / JSON 能不能解析、字段齐不齐、模型 id 与站点清单是否一致、Token 是不是本次这个、
默认模型是否指向存在的 provider 与 model。**预检不通过时，这次就跳过对应目标的写入**——
宁可少写一次，也不写坏文件。设置里可以关掉"同步前预检"，也可以随时用「校验写入内容」手动跑一次。

### 思考档（reasoning）怎么写的

DSH 在请求没带显式思考档时会回落到 provider 的 `reasoning`
（本体代码：`resolveReasoningLevel(model, options.reasoningEffort ?? profile.reasoning)`）。
如果这个值有哪个模型不支持，DSH 会直接报 `does not support reasoning effort "…"`，
所以程序按这两条规则写（**核心原则：你自己设的档位不动，只有不合法才纠正**）：

| 字段 | 程序怎么处理 |
| --- | --- |
| provider 级 `reasoning` | 你已经设了、而且所有已注册模型都支持 → **原样保留**；你设了但某个模型不支持（会直接报错）→ 改成所有模型都支持的公共档位；原本没有这个字段 → **不替你加**（新建 provider 时例外，会给一个公共档位兜底） |
| `agent-default-model` 的 `reasoningEffort` | 已有值合法 → **原样保留**；已有值非法 → 按 `high → max → medium → low → xhigh → minimal` 挑一个该模型支持的档位；原本缺失 → 不新增（只有程序刚自动创建默认模型时才写兜底值） |

也就是说：同步过一次之后，你在 DSH 里把思考强度调成任何合法值，程序都不会再改回去；
只有那个值会让 DSH 直接报错时才会被纠正（日志里会写清楚改了什么、为什么）。

预检只拦截“已有值明确不受支持”的情况，不会因为用户省略思考档而替他补值。

### DSH profile 范围

“统一所有 profile 到 madmodel”已经移除，不会再迁移成开启状态。程序现在按下面的顺序确定范围：

1. 设置里填了「受管 profile」→ 只处理这一份；
2. 留空 → 更新所有**解析后确实存在、且键名为 `madmodel` 的本程序 provider** 的 profile；
3. 一份都没有 → 只在一个 profile 中自动创建：优先 `desktop`，没有就 `web`，再没有就用按名称排序的第一份。

不会因为文件正文里出现清华域名或 `MADMODEL_API_KEY` 就认领整个 profile；`madmodel` 键被
别的配置占用时直接报错、不覆盖。自动创建 provider 时才会把该 profile 的默认模型指向它；
已有 provider 的默认模型和合法思考档保持不动。

注意：DSH 自己也会写这两份 profile 文件（在 DSH 设置里改东西时）。改完建议重启一次 DSH，
否则它可能用内存里的旧配置把文件覆盖回去。

### 其它可配置项

| 配置项 | 说明 |
| --- | --- |
| DSH 目录 / CC Switch 目录 | 留空 = 自动探测；也可以手动指定 |
| 启用 DSH 写入 / 启用 CC Switch 写入 | 两个目标可以分别关掉 |
| DSH 凭据 / Provider | 分别控制 `MADMODEL_API_KEY` 与 `llm-pi-ai` 的写入 |
| 受管 profile | 留空自动选择；也可指定 `desktop`、`web` 等 profile 名 |
| CC Switch 卡片 / 当前路由 | 分别控制自己的卡片与当前供应商/代理路由；路由激活**默认关闭** |
| CC Switch 运行中 | 默认「先提示我，由我决定」；也可显式改为直接改数据库 |
| 顺带刷新同站点旧卡片 | **默认关**（那会改动别人的卡片）；打开后只替换同站点卡片的 Token 字段 |
| 备份保留份数 | 每个文件最多留几份普通备份（默认 10）；**首次运行那份原始备份永远保留**，不占这个额度 |
| 同步前预检 | 默认开；关掉就跳过预检直接写 |
| 静默取 Token 超时 | 自动同步在后台等 Token 的上限（10–600 秒，默认 40） |
| 自动登录 | 默认关；密码只存 Windows Credential Manager。保存凭据后后台登录，验证码/二次验证时才显示窗口 |
| 允许最小对话校验 | 默认关，避免在用户不知情时消耗模型额度 |
| 自动同步间隔 | 默认 60 分钟，每次随机浮动 ±10 分钟 |
| DSH 默认模型 | 自动创建 provider/default-model 时优先使用哪个模型 id |
| 日志级别 | INFO / DEBUG（保存后立即生效） |
| 主界面日志 | 完整日志 / 仅同步结果与时间 / 关闭，默认完整 |
| 日志保留 | 旧日志保留 1–90 天，默认 7 天；单个文件 2 MiB × 3 轮转 |
| 窗口与关窗 | 主窗口和设置窗口会记住上次大小；点 `×` 最小化到托盘（默认）或直接退出程序 |
| 创建桌面快捷方式 | 在桌面生成一个指向本程序的快捷方式，双击即可启动 |
| 手动粘贴 Token | 内置浏览器不好使时的兜底：把站点"复制 token"的内容粘进来 |

## 常见情况

### 提示"上一次同步还没结束，忽略这次请求"

这是修掉的老 bug 场景（点 `X` 关掉登录窗口后状态没释放）。现在的行为：

- 关掉登录窗口 = 取消这次取 Token，状态立刻释放，可以直接再点同步；
- 真的还在忙（比如正在写文件）时点同步，会弹「强行终止并重试」/「取消」。
  「强行终止」是**安全停下**：正在写的那一份文件先原子写完，下一个步骤前才停，然后自动重试一次。

### 程序放在桌面上

程序**直接在桌面根目录**（不是桌面里的子文件夹）运行时，启动会提醒一次：
继续运行会在桌面生成并使用 `private` 数据文件夹，里面是 Token、设置、备份和浏览器会话，
**请不要删除**。三个选择：

| 选择 | 结果 |
| --- | --- |
| 选择 private 位置… | 把运行数据放到别处（推荐，比如 D 盘一个固定文件夹），以后不再提醒 |
| 仍然继续 | 保持现状，以后不再提醒 |
| 退出程序去换本程序位置 | 直接退出，不记状态（把程序挪到别处再运行即可） |

private 位置可以在设置里随时改（「备份、日志与维护 → 更改位置…」）。
会校验并拒绝：系统目录（Windows / System32）、Program Files、ProgramData、盘符根、
临时目录、桌面；换位置时把现有数据复制过去，重启后生效。
搬家和卸载都会先把浏览器档案、日志句柄、单实例锁放开，所以不会出现
`browser-profile / logs 删不掉`；**删除只移到回收站**（回收站不可用时保留原样并报告），
旧目录里只剩程序自己的东西时会连目录一起清掉，留着你自己文件的目录则保持原样。

### 想彻底清干净（干净卸载）

设置 →「备份、日志与维护 → 干净卸载…」，两次确认，并问你要不要
**把 DSH / CC Switch 还原到本程序第一次修改之前**。有字段级日志时只撤销本程序改过且未被你
改动的字段；没有日志的旧备份会在额外高风险提示后整文件还原。然后清掉程序自己的全部运行数据。

程序自身的 exe **不会**被删除，需要你手动删；界面上会告诉你具体路径。
自选 private 目录里属于你自己的文件一律不动，目录也不会被整个删掉。
全程只用正常的文件操作，不写自删除脚本、不调用 `rd` / `del` / `reg`，不会触发报毒行为。
删除统一走**回收站**（可撤销）；如果回收站不可用或文件仍被占着，
程序保持原样并报告，不会改成永久删除。

## 运行数据

所有隐私与运行数据都在程序目录下的 `private/`（不进入 Git）：

```
private/
  settings.json       你的设置
  state.json          运行状态（最近 Token 为 DPAPI 密文、时间戳）
  model-cache.json     站点模型清单的缓存（6 小时，加速下次同步）
  logs/               日志（2 MiB × 3 轮转，默认保留 7 天）
  backups/            DPAPI 加密的写入前备份与还原索引
  changes/            DPAPI 加密的字段级变更日志
  browser-profile/    内置浏览器独立用户目录（当前用户 ACL，保存登录会话）
```

如果程序目录不可写（例如装在只读位置），会自动改用
`%LOCALAPPDATA%\autoThuDeepSeekAPI\private\`。

private 的位置也可以自己改（设置 →「备份、日志与维护 → 更改位置…」）。选定后位置记在
`%LOCALAPPDATA%\autoThuDeepSeekAPI\private-location.json` 这个小指针文件里 ——
它不能放在 private 自己里面（否则换了位置就找不回来），也尽量不在桌面留文件；
「干净卸载」会把它一起删掉。指针指向的目录暂时不可用时，程序会退回默认位置并在日志里说明。

Windows Credential Manager 只保存你显式开启“自动登录”后的账号密码；不启用时不会写入。
Token 和备份的 DPAPI 密文只能由同一 Windows 账户解密。Qt WebEngine 的浏览器会话无法整体
加密，因此使用当前用户专属 ACL；如果多个 Windows 账户共用电脑，请各自使用自己的账户运行。

## 项目结构与开发

按这个顺序看最快（每份文档负责的事不重叠）：

| 文档 | 内容 |
| --- | --- |
| `README.md`（本文） | 怎么用、有哪些开关、边界与限制 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 每个模块负责什么、数据怎么流动、改代码要注意的坑 |
| [`CHANGELOG.md`](CHANGELOG.md) | 每个版本改了什么、修了哪些 bug |
| [`docs/PROGRESS.md`](docs/PROGRESS.md) | 当前进度、待办清单、怎么回退到旧版本 |
| [`docs/REQUIREMENTS-2.5.md`](docs/REQUIREMENTS-2.5.md) | 2.5 这一轮的需求、决策与验收清单 |
| [`releases/README.md`](releases/README.md) | 打包产物怎么留档与重建（产物本身不进 Git） |

目录速查：

```
src/auto_thu_deepseek_api/
  app.py            程序入口：设置、日志、主窗口、定时同步
  settings.py       用户设置与版本迁移
  crypto.py         Windows DPAPI 加解密
  credentials.py    Windows Credential Manager（可选自动登录）
  http_tls.py       HTTPS 证书上下文与 Windows 证书库回退
  sync.py           同步编排：校验 Token → 逐目标备份写入
  preflight.py      写入前预检（只读干跑）
  backup.py         加密备份、清理、旧版整文件还原
  change_journal.py 字段级变更日志与安全撤销
  backup_ops.py     手动备份 / 首次自动备份（界面用）
  startup.py        开机启动、桌面快捷方式
  token/            内置浏览器取 Token、JWT、站点校验、模型清单
  targets/          DSH 与 CC Switch 的探测与写入
  ui/               主窗口、托盘、设置对话框、后台线程
tests/              单元测试（当前 190 项）
tools/              图标生成、界面截图、打包诊断等小工具
build/              PyInstaller 规格与打包脚本
private/            运行数据（Token、日志、备份、浏览器会话），不进 Git
```

跑测试与截图（都在项目根目录执行）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
.\.venv\Scripts\python.exe tools\screenshot_ui.py                # 主界面截图
.\.venv\Scripts\python.exe tools\screenshot_ui.py --settings     # 设置对话框截图
.\.venv\Scripts\python.exe tools\screenshot_ui.py --settings --tab=3
```

### 从源码运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m auto_thu_deepseek_api
```

（源码运行时需要把 `src` 加入 `PYTHONPATH`，或在项目根目录执行
`$env:PYTHONPATH="src"`。）

## 打包

```powershell
.\build\build_app.ps1
```

会同时产出文件夹版（`dist/autoThuDeepSeekAPI/`，推荐日常使用）与单文件版
（`dist/autoThuDeepSeekAPI.exe`）。

打包中间产物落在 `build/_work/`，两个版本最后再拷进 `dist/`；这样 `dist/` 里不会出现
“没有 `_internal` 的启动器 exe”那种一点就失败的半成品。

打包脚本用 `build/atd.spec`，里面挡掉了两类会“抢名字”的外部 DLL（UCRT 转发 DLL 与
外部工具链的 ICU），否则 Qt 在打包后会报 `DLL load failed while importing QtCore:
找不到指定的程序`。改动 spec 前请先看它的文件头说明。

EXE 会嵌入 `build/version_info.txt` 中的产品名、版本和公司信息。当前没有 Authenticode
代码签名证书，因此 Windows 仍可能显示“未知发布者”；代码签名需要额外证书，不是打包脚本能
凭空生成的。2.8.0 单文件 EXE 约 164 MiB，可直接作为 GitHub Release 附件（Release assets 的限额
高于普通 Git 仓库单文件 100 MiB 限制）。

体积主要来自 Qt WebEngine。程序不会为了压缩而移除登录所需的 Chromium 组件；浏览器档案只在
取 Token 时启动，流程结束会关闭窗口并释放浏览器 profile，不再在托盘里常驻 Chromium；
文件夹版 2.8.0 约 429 MiB、2,838 个文件，启动更快，日常使用推荐。

### 打包后自检

```powershell
.\dist\autoThuDeepSeekAPI\autoThuDeepSeekAPI.exe --selftest
```

会起一个本地网页、把一枚假 Token 写进浏览器存储，验证“内置浏览器 + 捕获链路”与
Python HTTPS 证书上下文随包可用，通过时以退出码 0 结束。排查启动问题时可以加上
`ATD_CONSOLE=1` 环境变量重新打包，得到带控制台的版本。

## 说明与边界

- 默认不模拟登录。只有你显式开启自动登录并保存凭据时，程序才在 `*.tsinghua.edu.cn`
  页面后台自动填充、提交；验证码、二次验证、MFA、账号密码错误或页面改版时才会显示
  登录窗口。清华登录页隐藏的验证码框不会被误判成需要人工验证。
- Token 由站点前端签发，有效期约 5 小时（以站点实际为准）；会话过期后需要你重新登录一次。
- Token 校验优先走站点的会话接口（`GET /model-api/auth-login`），不消耗模型额度；
  只有你显式允许时才兜底发一次最小对话请求。默认在无法判断时如实记为"未能校验"。
- Python HTTPS 默认校验站点证书；若精简运行环境缺少默认 CA 文件，会回退到 Windows
  证书库，仍保持证书链和主机名校验，不会关闭 TLS 验证。
- 模型列表跟着站点走：读站点前端包里的模型清单（缓存 6 小时），`contextWindow` 统一 1000000，
  并排除站点上那两个不注册的模型；读不到才用内置名单。DSH 里**只认键名为 `madmodel`
  且标记属于本程序**的 provider（站点地址 / 我们的 `apiKeyEnv` / 该键下的本地转发）。
  只合并 `apiKeyEnv` 与 `models`。
  **光有 `apiKeyEnv: MADMODEL_API_KEY` 不算数**（那可能是旧版本误写进去的残留），
  **别人的 provider（ollama、isearcher、GLM…）一律不碰**；扫到"带着我们的标记却不是我们的"
  provider 会提醒你去检查/从备份还原，但程序不会改它。`madmodel` 这个键被别的配置占用时
  拒绝写入并报错，不会覆盖它。
- CC Switch 正在运行时直接改数据库有被覆盖的风险。默认策略是**先提示用户**。
- 更新自己的卡片与激活当前路由是两个独立开关；路由激活默认关闭。路由写入失败时，本次
  已写入的卡片和 `settings.json` 会按变更日志回滚。
- 写入是“无变化就不动”：卡片、当前路由都已是最新时，不会备份、也不会碰数据库。

