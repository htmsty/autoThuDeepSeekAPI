# 图标素材来源

`assets/icon.png`、`assets/icon.ico`、`assets/icon-master.png` 由 `tools/make_icon.py`
把以下三家标识组合而成，仅用于标明本程序对接的对象：

| 素材 | 来源 | 说明 |
| --- | --- | --- |
| `logos/openai.svg` | Wikimedia Commons 的 ChatGPT / OpenAI 花瓣标识 | OpenAI、Codex 是 OpenAI 的商标 |
| `logos/deepseek.svg` | Simple Icons 的 DeepSeek 标识（CC0） | DeepSeek 及其鲸鱼标识归 DeepSeek 所有 |
| `logos/ccswitch.png` | CC Switch 官方仓库 `src-tauri/icons/icon.png` | CC Switch 标识归其作者所有 |

三家的名称与标识归各自权利人所有；本仓库不声称对它们拥有任何权利，
也不代表三家对本程序的认可。

## TLS 兼容证书

`codex-rejected-cas.pem` 是当前构建运行时 `ssl.py` 要求放在模块目录旁的兼容数据文件。
它是公开根证书数据，不含账号、私钥或本机路径；发行包会同时放在 `_MEIPASS` 根目录，
用于避免精简 Python 创建 HTTPS 上下文时因缺少该文件而失败。
