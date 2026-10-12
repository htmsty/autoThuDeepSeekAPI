# autoThuDeepSeekAPI

自动获取清华大学 MadModel 平台（<https://madmodel.cs.tsinghua.edu.cn/>）的 API Token，
并写入 DeepSeek Harness（DSH）与 CC Switch 的配置。Windows 便携程序，双击即用。

- 作者：htmsty
- 许可证：AGPL-3.0（见 [LICENSE](LICENSE)）
- 设计与需求备忘：[`../DESIGN_AND_REQUIREMENTS.md`](../DESIGN_AND_REQUIREMENTS.md)

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

## 界面与托盘

- 主界面：立即获取并同步、打开配置、打开日志、开机启动（最小化到托盘）、启动时立即同步、
  自动同步（间隔分钟数自定）、同步完成立即最小化到托盘、退出；并显示 DSH / CC Switch
  安装状态、Token 状态与程序日志。
- 点击 `×` 最小化到系统托盘并从任务栏消失；托盘图标左键单击打开主界面，右键单击弹出菜单。

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
- Token 校验优先走站点的会话接口（`GET /model-api/auth-login`，快且不消耗模型额度）；
  只有它给不出结论时才会兜底发一次最小对话请求，连不上站点则如实记成"未能校验"而不是失败。
- CC Switch 正在运行时直接改它的数据库有被覆盖的风险，因此默认会先提示你关闭它
  （该提示可以关掉，也可以选择"直接改"）。
- 写入是"无变化就不动"：卡片、当前路由都已是最新时，不会备份、也不会碰数据库。

