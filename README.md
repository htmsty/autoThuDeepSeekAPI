# autoThuDeepSeekAPI

自动获取清华大学 MadModel 平台（<https://madmodel.cs.tsinghua.edu.cn/>）的 API Token，
并写入 DeepSeek Harness（DSH）与 CC Switch 的配置。Windows 便携程序，双击即用。

**?!无限Token?!**

- 作者：htmsty
- 许可证：AGPL-3.0（见 [LICENSE](LICENSE)）

## 它做什么

1. 在程序**内置的浏览器窗口**里打开清华统一身份认证，由你自己完成登录（程序不做模拟点击）；
2. 登录完成后程序自动捕获 Token、校验有效性，并**自动关闭**该窗口；
3. 把 Token 写入你启用的目标：
   - DSH 凭据 `%USERPROFILE%\.dsh\.credentials.yaml` 的 `MADMODEL_API_KEY`；
   - DSH 供应商 `%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml` 的
     `llm-pi-ai.providers.madmodel`（名称 `DeepSeek (THU)`，地址
     `https://madmodel.cs.tsinghua.edu.cn/v1`）；
   - CC Switch 数据库 `%USERPROFILE%\.cc-switch\cc-switch.db` 的供应商卡片
     `deepseek-thu / DeepSeek (THU)`，并顺手刷新同站点旧卡片的 Token；
   - CC Switch 当前路由 `settings.json`、`live-state.json` 指向 `DeepSeek (THU)`。
4. 按你设定的间隔自动同步；写入前自动备份，可一键还原。

## 首次运行的自动备份

程序第一次运行时，会先把 DSH 凭据、各 profile、CC Switch 数据库与路由文件各备份一份，
这份**原始备份**带着 `original` 标记，**不会**随备份次数增多而被清理掉；之后每次写入前
的普通备份才按"备份保留份数"轮换。想回到"程序第一次动它之前"的状态，主界面点「一键还原」
或在设置里点「还原到首次修改前」即可（还原前会先给当前状态再留一份，所以还原本身也能退回来）。

## 界面与托盘

- 主界面：立即获取并同步、打开配置、打开日志、开机启动（最小化到托盘）、启动时立即同步、
  自动同步（默认开启、间隔 180 分钟，可自定）、同步完成立即最小化到托盘、退出；并显示
  DSH / CC Switch 安装状态、Token 状态（显示"有效至 …"）与程序日志。
- 第一次运行、还没有任何缓存 Token 时，"启动时立即同步"会直接打开浏览器窗口让你登录，
  不再先静默等一轮超时；之后有缓存就静默取。
- 点击 `×` 最小化到系统托盘并从任务栏消失；托盘图标左键单击打开主界面，右键单击弹出菜单。

### 主界面按钮

| 按钮 | 作用 |
| --- | --- |
| 立即获取并同步 | 打开内置浏览器窗口（已经登录过就直接静默取），拿到 Token 后校验并写入启用的目标 |
| 校验写入内容 | **只读**预检：把将要写入 DSH / CC Switch 的内容渲染出来逐项校验，不动任何文件 |
| 手动备份 | 立刻把 DSH 凭据、各 profile、CC Switch 数据库与路由文件各留一份备份 |
| 一键还原 | 把各文件恢复成最早的备份（= 第一次修改前的状态）；还原前会先把当前状态再留一份 |
| 打开配置 / 打开日志 / 打开数据目录 | 打开设置对话框、日志目录、`private/` 数据目录 |
| 退出 | 真正退出程序（点 `×` 默认只是最小化到托盘，可在设置里改成直接退出） |

### 写入前预检

同步前会先把"将要写出去的内容"干跑渲染一遍（和真正写盘用的是同一套代码，只是不备份不落盘），
再校验：YAML / JSON 能不能解析、字段齐不齐、模型 id 与站点清单是否一致、Token 是不是本次这个、
默认模型是否指向存在的 provider 与 model。**预检不通过时，这次就跳过对应目标的写入**——
宁可少写一次，也不写坏文件。设置里可以关掉"同步前预检"，也可以随时用「校验写入内容」手动跑一次。

### 思考档（reasoning）怎么写的

DSH 在请求没带显式思考档时会回落到 provider 的 `reasoning`
（本体代码：`resolveReasoningLevel(model, options.reasoningEffort ?? profile.reasoning)`）。
如果这个值有哪个模型不支持，DSH 会直接报 `does not support reasoning effort "…"`，
所以程序按这两条规则写：

- provider 级 `reasoning` 只写**所有已注册模型都支持**的档位（取交集；当前两个模型的交集是
  `low`）。没有公共档位就干脆不写，交给 pi-ai 自己决定。你自己往 provider 里加模型后，
  下次同步会重新算一遍。
- `agent-default-model` **总是**带一个显式且合法的 `reasoningEffort`：已有值合法就保留，
  非法或缺失时按 `high → max → medium → low → xhigh → minimal` 挑一个该模型支持的档位
  （`DeepSeek-V4.1-Flash` 会选 `high`；它同时支持 `max`）。

这两条也进了预检：不满足就拦住这次写入，不会再出现"第一次运行发消息就报思考档不支持"。

### 设置里的「统一所有 profile 到 madmodel」

**默认是勾上的**（想让 DSH 用官方 DeepSeek 或自己挑模型时再取消；取消后不会被重新勾上，
只有从 0.1.4 之前的版本升级上来时会自动勾选一次）。

| 勾选情况 | 程序会做什么 |
| --- | --- |
| 不勾 | 只更新**已经配置过 madmodel** 的 profile（正文里有清华站点域名、`madmodel:` 或 `MADMODEL_API_KEY` 才算）：写 `apiKeyEnv` 与 `models`。**默认模型保持你在 DSH 里选的那个**，其它 profile 一律不碰。 |
| 勾上 | 所有 profile 都写，并把 `agent-default-model` 强制切到 `madmodel`，同时把 `model` 校正成 madmodel 下真实存在的模型 id。 |

所以：想让"每次同步都把 DSH 切到 DeepSeek (THU)"就**勾上**；想让 DSH 用官方 DeepSeek
或自己挑模型就**不要勾**。两种情况的共同点是：provider 只按 `apiKeyEnv` / `baseURL`
认领，认不出才新建，`madmodel` 键被别人占用时直接报错、不覆盖。

注意：DSH 自己也会写这两份 profile 文件（在 DSH 设置里改东西时）。改完建议重启一次 DSH，
否则它可能用内存里的旧配置把文件覆盖回去。

### 其它可配置项

| 配置项 | 说明 |
| --- | --- |
| DSH 目录 / CC Switch 目录 | 留空 = 自动探测；也可以手动指定 |
| 启用 DSH 写入 / 启用 CC Switch 写入 | 两个目标可以分别关掉 |
| CC Switch 运行中 | 默认不提示、直接改数据库；想每次先确认就取消勾选"不再弹提醒" |
| 备份保留份数 | 每个文件最多留几份普通备份（默认 10）；**首次运行那份原始备份永远保留**，不占这个额度 |
| 同步前预检 | 默认开；关掉就跳过预检直接写 |
| 静默取 Token 超时 | 自动同步在后台等 Token 的上限（10–600 秒，默认 40） |
| DSH 默认模型 | 统一 profile 时写进 `agent-default-model` 的模型 id |
| 日志级别 | INFO / DEBUG（改完重启生效） |
| 关窗行为 | 点 `×` 最小化到托盘（默认）或直接退出程序 |
| 创建桌面快捷方式 | 在桌面生成一个指向本程序的快捷方式，双击即可启动 |
| 手动粘贴 Token | 内置浏览器不好使时的兜底：把站点"复制 token"的内容粘进来 |

## 运行数据

所有隐私与运行数据都在程序目录下的 `private/`（不进入 Git）：

```
private/
  settings.json       你的设置
  state.json          运行状态（最近 Token、时间戳）
  logs/               日志
  backups/            写入前的备份与还原索引
  browser-profile/    内置浏览器的独立用户目录（保存登录会话）
```

如果程序目录不可写（例如装在只读位置），会自动改用
`%LOCALAPPDATA%\autoThuDeepSeekAPI\private\`。

## 从源码运行

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

### 打包后自检

```powershell
.\dist\autoThuDeepSeekAPI\autoThuDeepSeekAPI.exe --selftest
```

会起一个本地网页、把一枚假 Token 写进浏览器存储，验证“内置浏览器 + 捕获链路”随包可用，
通过时打印 `自检通过：抓到 Token` 并以退出码 0 结束。排查启动问题时可以加上
`ATD_CONSOLE=1` 环境变量重新打包，得到带控制台的版本。

## 说明与边界

- 程序不模拟鼠标键盘，也不代替你点击页面：登录永远由你本人在内置浏览器窗口里完成。
- Token 由站点前端签发，有效期约 5 小时（以站点实际为准）；会话过期后需要你重新登录一次。
- Token 校验优先走站点的会话接口（`GET /model-api/auth-login`）；
  只有它给不出结论时才会兜底发一次最小对话请求，连不上站点则如实记成"未能校验"而不是失败。
- 模型列表跟着站点走：读站点前端包里的模型清单（缓存 6 小时），`contextWindow` 统一 1000000，
  并排除站点上那两个不注册的模型；读不到才用内置名单。DSH 里已有的 provider 按 `apiKeyEnv` /
  `baseURL`（含本地转发 `127.0.0.1:…/v1`）认领，只合并 `apiKeyEnv` 与 `models`；
  `madmodel` 这个键被别的配置占用时拒绝写入并报错，不会覆盖它。
- CC Switch 正在运行时直接改它的数据库有被覆盖的风险。程序现在默认**不弹提示、直接改**
  （设置里取消勾选"不再弹提醒"就能恢复提示）。想更稳妥的话，先手动退出 CC Switch 再同步。
- 写入是"无变化就不动"：卡片、当前路由都已是最新时，不会备份、也不会碰数据库。

