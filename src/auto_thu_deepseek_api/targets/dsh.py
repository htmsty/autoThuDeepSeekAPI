"""DeepSeek Harness（DSH）配置写入。"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from ruamel.yaml import YAML

from .. import constants
from ..backup import BackupManager
from ..paths import TargetPaths
from ..token.jwt_utils import fingerprint
from .base import ApplyResult, RenderedFile, TargetStatus, emit, write_with_rollback

logger = logging.getLogger(__name__)

# 站点的模型 id 长这样：DeepSeek-V4.1-Flash、qwen3.8-27b。
# 出现空格、括号、逗号这类字符多半是字段被填串了（显示名填进了 id），要修掉。
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$")


def _yaml(style_from: str | None = None) -> YAML:
    """往返模式的 YAML 读写。

    `style_from` 给的是文件原始文本：如果它顶层是"顶格写 `- `"的风格（DSH 自己写出来的
    就是这种），dump 时也保持这个风格，别把整份文件重排一遍。
    """

    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    if style_from and re.search(r"(?m)^- ", style_from):
        yaml.indent(mapping=2, sequence=2, offset=0)
    else:
        yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


class DshTarget:
    key = "dsh"
    label = "DeepSeek Harness (DSH)"

    def __init__(
        self,
        paths: TargetPaths,
        unify_all_profiles: bool = False,
        default_model_id: str = "",
    ) -> None:
        self.paths = paths
        self.unify_all_profiles = unify_all_profiles
        self.default_model_id = (default_model_id or "").strip()
        # 干跑：只把"将要写入的内容"渲染出来，不备份也不落盘（预检用）
        self.dry_run = False
        self.rendered: list[RenderedFile] = []

    # —— 探测 ——

    def detect(self) -> TargetStatus:
        base = self.paths.base_dir
        credentials = self.paths.credentials_file
        profiles = self.paths.profiles_dir
        if base.is_dir() and ((credentials and credentials.exists()) or (profiles and profiles.is_dir())):
            detail = f"找到 DSH 目录：{base}"
            if credentials and credentials.exists():
                detail += "；凭据文件存在"
            if profiles and profiles.is_dir():
                count = len(list(profiles.glob(f"*/{constants.DSH_PATCH_FILE}")))
                detail += f"；{count} 个 profile"
            return TargetStatus(self.key, self.label, True, base, detail)
        return TargetStatus(self.key, self.label, False, base, f"未找到 DSH 配置：{base}")

    # —— 写入 ——

    def apply(self, token: str, models: list[dict], backup: BackupManager) -> ApplyResult:
        result = ApplyResult(self.key, self.label)
        credentials = self.paths.credentials_file
        if credentials and credentials.exists():
            try:
                changed = self._write_credentials(credentials, token, backup, result)
                result.changed = result.changed or changed
            except Exception as exc:  # noqa: BLE001 - 写入失败要如实报告
                result.fail(f"凭据写入失败：{exc}")
        else:
            result.note(f"未找到凭据文件，跳过：{credentials}")

        self._write_profiles(models, backup, result)
        return result

    def _write_credentials(
        self,
        path: Path,
        token: str,
        backup: BackupManager,
        result: ApplyResult,
    ) -> bool:
        text = path.read_text(encoding="utf-8")
        yaml = _yaml(text)
        rebuilt = False
        try:
            data = yaml.load(text)
        except Exception as exc:  # noqa: BLE001 - 文件已经坏了，下面按行救回能救的
            result.note(f"凭据文件解析失败（{type(exc).__name__}），已留底并尝试重建")
            salvaged = self._salvage_credentials(text)
            if not salvaged:
                result.fail(f"凭据文件损坏且没有可救回的键，请手工修复：{path}")
                return False
            result.note(f"从损坏的文件里救回 {len(salvaged)} 个键：{', '.join(sorted(salvaged))}")
            data = {"version": 1, "refs": salvaged}
            rebuilt = True
        if not isinstance(data, dict):
            raise ValueError("凭据文件结构不是映射（mapping）")

        refs = data.get("refs")
        if refs is None:
            data["refs"] = {}
            refs = data["refs"]
        if not isinstance(refs, dict):
            raise ValueError("凭据文件的 refs 不是映射")

        if not rebuilt and refs.get(constants.DSH_CREDENTIAL_KEY) == token:
            # 这句只说明"文件里的值和本次拿到的一样、不用改写"，不涉及任何网络请求；
            # Token 有效性由 sync.run_sync 里的「Token 校验」负责，所以这里带上指纹方便对账。
            result.note(
                "凭据里的 MADMODEL_API_KEY 与本次相同（指纹 "
                f"{fingerprint(token)}），无需改写；本目标不做网络校验"
            )
            return False

        label = "dsh-credentials-broken" if rebuilt else "dsh-credentials"
        refs[constants.DSH_CREDENTIAL_KEY] = token

        import io

        stream = io.StringIO()
        yaml.dump(data, stream)
        new_text = stream.getvalue()

        # 能只改一行就只改一行：凭据文件里还有别的工具（DeepSeek / 清华）的 key，
        # 整份重写会把它们的排版一起重排 —— 那也算动了用户的配置。
        # 注意：文件是"坏掉重建"时必须整份写新结构，不能往坏文本里贴一行。
        if not rebuilt:
            spliced = self._replace_ref_line(text, constants.DSH_CREDENTIAL_KEY, token)
            if spliced is not None:
                new_text = spliced

        # 回读校验：确认写进去的就是这个 Token（避免折行标量之类的坑）
        from ruamel.yaml import YAML as CheckYAML

        checker = CheckYAML(typ="safe")
        parsed = checker.load(new_text)
        if not isinstance(parsed, dict) or parsed.get("refs", {}).get(constants.DSH_CREDENTIAL_KEY) != token:
            raise ValueError("回读校验失败：写入后解析出的 Token 与预期不一致")

        emit(self, path, new_text, label, "yaml")
        if self.dry_run:
            result.note(f"（预检）将更新凭据 {path}")
            return True

        # 真实写入前先留底（重建的情况留的就是那份坏文件）；写坏了自动回滚
        ok, message = write_with_rollback(path, new_text, backup, label)
        if not ok:
            result.fail(message)
            return False
        result.note(f"已更新凭据 {path}")
        logger.info("DSH 凭据已更新：%s", path)
        return True

    @staticmethod
    def _salvage_credentials(text: str) -> dict[str, str]:
        """从坏掉的凭据文件里按行捞回 `大写键: 值`，只认键名全大写的引用。

        DSH 的 refs 键都长这样（DEEPSEEK_API_KEY / MADMODEL_API_KEY / TSINGHUA_API_KEY），
        而 records/payload 里的 kind、secret、id 不会命中，所以不会把会话材料混进 refs。
        """

        salvaged: dict[str, str] = {}
        for line in text.splitlines():
            match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*:\s*(\S+)\s*$", line)
            if not match:
                continue
            key, value = match.group(1), match.group(2).strip("'\"")
            if value and value.lower() not in {"null", "~"}:
                salvaged[key] = value
        return salvaged

    # —— profile 补丁 ——

    def _profile_files(self) -> list[Path]:
        profiles_dir = self.paths.profiles_dir
        if not profiles_dir or not profiles_dir.is_dir():
            return []
        files = sorted(profiles_dir.glob(f"*/{constants.DSH_PATCH_FILE}"))
        return [path for path in files if path.is_file()]

    def _in_scope(self, path: Path) -> bool:
        if self.unify_all_profiles:
            return True
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        # 只有"明确就是我们"的 profile 才在范围内：正文里有清华站点域名，或有 madmodel 这个 provider 键。
        # 早先还看"正文里有没有 MADMODEL_API_KEY"——可这个标记可能是旧版本误写到别人的 provider
        # （例如 isearcher / ollama）身上的，于是会把别人的 profile 也拉进来处理，必须去掉。
        # 另外 "madmodel:" 必须出现在**键的位置**（行首缩进后），注释里提到它不算。
        key_line = re.compile(
            rf"(?m)^\s*['\"]?{re.escape(constants.DSH_PROVIDER_KEY)}['\"]?\s*:"
        )
        return constants.MADMODEL_HOST in text or key_line.search(text) is not None

    def _write_profiles(
        self,
        models: list[dict],
        backup: BackupManager,
        result: ApplyResult,
    ) -> None:
        files = self._profile_files()
        if not files:
            result.note("没有找到 profile 补丁文件，跳过供应商写入")
            return

        candidates = [path for path in files if path.parent.name != "" and path.parent.name not in {"node_modules"}]
        scoped = [path for path in candidates if self._in_scope(path)]
        if not scoped:
            result.note(
                "没有 profile 已配置 madmodel（默认只写这类 profile；"
                "需要全部统一请在设置里勾选「统一所有 profile 到 madmodel」）"
            )
            return

        for path in scoped:
            try:
                changed = self._patch_profile(path, models, backup, result)
                result.changed = result.changed or changed
            except Exception as exc:  # noqa: BLE001
                result.fail(f"{path.parent.name} 写入失败：{exc}")

    def _patch_profile(
        self,
        path: Path,
        models: list[dict],
        backup: BackupManager,
        result: ApplyResult,
    ) -> bool:
        original = path.read_text(encoding="utf-8")
        yaml = _yaml(original)
        data = yaml.load(original)
        if not isinstance(data, list):
            raise ValueError("补丁文件顶层不是数组")

        entry = self._find_entry(data, constants.DSH_LLM_ENTRY_ID, constants.DSH_LLM_ENTRY_NAME)
        if entry is None:
            entry = {"id": constants.DSH_LLM_ENTRY_ID, "name": constants.DSH_LLM_ENTRY_NAME, "config": {}}
            data.append(entry)
            result.note(f"{path.parent.name}：原来没有 llm-pi-ai 条目，已新建")

        config = entry.get("config")
        if not isinstance(config, dict):
            config = {}
            entry["config"] = config
        providers = config.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            config["providers"] = providers

        # 先按"这个 provider 是不是我们的"去找：apiKeyEnv 认领，其次 baseURL 指向清华站点。
        # 这样即使 DSH 里把 madmodel 走本地转发（http://127.0.0.1:xxxx/v1）也能认出来，
        # 不会在旁边再建一个重复的 provider。
        found = self._find_provider(providers)
        name = path.parent.name
        created_here = False
        self._warn_about_suspicious_providers(
            providers, ours_key=found[0] if found else "", profile_name=name, result=result
        )

        if found is None:
            occupied = providers.get(constants.DSH_PROVIDER_KEY)
            if occupied is not None and not isinstance(occupied, dict):
                result.fail(
                    f"{name}：providers.madmodel 已被其它配置占用"
                    "（它的 apiKeyEnv 与 baseURL 都不指向清华站点），按约定不覆盖它"
                )
                return False
            if isinstance(occupied, dict) and occupied:
                result.fail(
                    f"{name}：providers.madmodel 已被其它配置占用"
                    "（它的 apiKeyEnv 与 baseURL 都不指向清华站点），按约定不覆盖它"
                )
                return False
            provider: dict = {}
            providers[constants.DSH_PROVIDER_KEY] = provider
            provider["displayName"] = constants.PROVIDER_DISPLAY_NAME
            provider["api"] = "openai-completions"
            provider["baseURL"] = constants.MADMODEL_API_BASE
            provider_key = constants.DSH_PROVIDER_KEY
            created_here = True
            result.note(f"{name}：已新建 provider madmodel")
        else:
            key, provider = found
            provider_key = key
            result.note(f"{name}：已找到 provider {key}")

        # 整个补丁文件里除"我们这一条（llm-pi-ai 里的 provider_key）"以外的 provider 都算别人的：
        # 只要还有别人，就绝不允许整份重写（那会重排他们的排版），宁可这次不写并报错。
        our_entry = self._find_entry(data, constants.DSH_LLM_ENTRY_ID, constants.DSH_LLM_ENTRY_NAME)
        foreign_providers: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            item_config = item.get("config")
            item_providers = item_config.get("providers") if isinstance(item_config, dict) else None
            if not isinstance(item_providers, dict):
                continue
            for key in item_providers:
                if item is our_entry and str(key) == provider_key:
                    continue
                foreign_providers.add(str(key))
        other_provider_keys = sorted(foreign_providers)

        # 硬护栏：**绝不改别的模型的配置**。
        # 只有"这个 provider 的标记就是我们"（或域名就是清华站点）才继续写；
        # 它的 apiKeyEnv 另有其人时宁可报错退出，也不动它一个字。
        existing_env = provider.get("apiKeyEnv")
        existing_base = str(provider.get("baseURL") or "")
        if (
            not created_here
            and isinstance(existing_env, str)
            and existing_env.strip()
            and existing_env != constants.DSH_CREDENTIAL_KEY
            and not self._is_madmodel_url(existing_base)
        ):
            result.fail(
                f"{name}：provider {provider_key} 用的是 {existing_env}，不是本程序的标记，"
                "按约定不动别的模型配置（要交给本程序维护，请把它改成 "
                f"{constants.DSH_CREDENTIAL_KEY} 的 provider，或换一个 provider 键）"
            )
            return False

        # 只合并我们负责的字段：apiKeyEnv 与 models。其余字段只在缺失时补上，
        # 不覆盖用户在 DSH 里自己对名称/地址/思考档的调整。
        provider_before = copy.deepcopy(provider)
        provider["apiKeyEnv"] = constants.DSH_CREDENTIAL_KEY
        if not provider.get("api"):
            provider["api"] = "openai-completions"
        if not provider.get("baseURL"):
            provider["baseURL"] = constants.MADMODEL_API_BASE

        merged = self._merge_models(provider.get("models"), models)
        if merged:
            provider["models"] = merged

        # provider 级 reasoning 是"请求没带思考档时的兜底默认"——DSH 里是
        # resolveReasoningLevel(model, options.reasoningEffort ?? profile.reasoning)。
        # 规则（用户要求：别改用户设的档位，除非不合法）：
        #   · 用户已经设了、而且所有模型都支持 → 原样保留，不动；
        #   · 用户设了但某些模型不支持 → 会直接报 UNSUPPORTED_REASONING_EFFORT，必须纠正；
        #   · 原本没有这个字段 → 不替他加（除非 provider 是我们这次刚新建的，那才给个兜底）。
        safe_reasoning = self._safe_provider_reasoning(merged or [])
        current_reasoning = provider.get("reasoning")
        has_reasoning = isinstance(current_reasoning, str) and bool(current_reasoning.strip())
        if created_here and not has_reasoning:
            if safe_reasoning:
                provider["reasoning"] = safe_reasoning
        elif has_reasoning and not self._reasoning_ok_for_all(str(current_reasoning), merged or []):
            if safe_reasoning:
                provider["reasoning"] = safe_reasoning
                result.note(
                    f"{name}：provider 的 reasoning={current_reasoning} 有模型不支持，"
                    f"已改成公共档位 {safe_reasoning}"
                )
            else:
                provider.pop("reasoning", None)
                result.note(f"{name}：provider 的 reasoning={current_reasoning} 有模型不支持，已删除")

        # 名称是空、或被写成了某个模型 id（DSH 界面把字段填串的典型样子）才纠正，
        # 用户自己起的名字不动。
        display_name = str(provider.get("displayName") or "").strip()
        model_ids = {str(model.get("id")) for model in (merged or [])}
        if not display_name or display_name in model_ids:
            provider["displayName"] = constants.PROVIDER_DISPLAY_NAME

        provider_changed = provider != provider_before

        # 数据层确认 agent-default-model 条目原本在不在：文本层万一定位不到它，
        # 只有"原本就没有"才允许追加缺的那块，否则会写出两条同名条目。
        entry_existed_before = (
            self._find_entry(
                data,
                constants.DSH_DEFAULT_MODEL_ENTRY_ID,
                constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
            )
            is not None
        )
        if self.unify_all_profiles:
            entry_changed = self._point_default_model(data, merged, provider_key, result)
        else:
            entry_changed = self._repair_default_model(data, merged, provider_key)

        import io

        stream = io.StringIO()
        yaml.dump(data, stream)
        new_text = stream.getvalue()
        # 只按"值"判断有没有变化：ruamel 整份重写会把别人 provider 的排版（流式列表空格、
        # 注释对齐）也改掉，那种"纯格式变化"不该算改动，更不该落盘。
        if not provider_changed and not entry_changed:
            result.note(f"{path.parent.name}：供应商配置已是最新")
            return False

        # 只把**我们要改的那两块**（我们的 provider / 默认模型条目）换成新文本，
        # 其余部分原样照抄 —— 这样别人 provider 的注释、引号、流式写法一个字节都不会变。
        final_text = self._keep_other_blocks_verbatim(
            original,
            new_text,
            provider_key=provider_key,
            provider_changed=provider_changed,
            entry_changed=bool(entry_changed),
            provider_created=created_here,
            entry_existed=entry_existed_before,
            allow_full_rewrite=not other_provider_keys,
        )

        # 落盘前先确认 dump 出来的是合法的顶层数组，且我们的 provider 真在里面。
        # 这里用往返模式解析：profile 允许 !!js 标签，safe 模式会拒收。
        from ruamel.yaml import YAML as CheckYAML

        check = CheckYAML(typ="rt").load(final_text)
        if not isinstance(check, list):
            raise ValueError("回读校验失败：写出的补丁不是顶层数组")
        check_entry = self._find_entry(check, constants.DSH_LLM_ENTRY_ID, constants.DSH_LLM_ENTRY_NAME)
        check_providers = (check_entry or {}).get("config", {}).get("providers", {})
        check_provider = check_providers.get(provider_key) if isinstance(check_providers, dict) else None
        if not isinstance(check_provider, dict) or check_provider.get("apiKeyEnv") != constants.DSH_CREDENTIAL_KEY:
            raise ValueError(f"回读校验失败：写出的补丁里找不到 provider {provider_key}")

        emit(self, path, final_text, "dsh-profile", "yaml")
        if self.dry_run:
            result.note(f"（预检）将更新供应商配置 {path}")
            return True

        ok, message = write_with_rollback(path, final_text, backup, "dsh-profile")
        if not ok:
            result.fail(message)
            return False
        result.note(f"已更新供应商配置 {path}")
        logger.info("DSH profile 已更新：%s", path)
        return True

    @staticmethod
    def _find_entry(data: list, entry_id: str, entry_name: str) -> dict | None:
        for item in data:
            if isinstance(item, dict) and (item.get("id") == entry_id or item.get("name") == entry_name):
                return item
        return None

    # —— 找到"我们那个" provider ——

    @staticmethod
    def _url_host(value: str) -> str:
        try:
            return (urlsplit(value).hostname or "").lower()
        except ValueError:
            return ""

    @classmethod
    def _warn_about_suspicious_providers(
        cls,
        providers: dict,
        *,
        ours_key: str,
        profile_name: str,
        result,  # noqa: ANN001 - ApplyResult
    ) -> None:
        """`带着我们的 apiKeyEnv、却明显不是我们的 provider` 要提醒用户去修。

        这种多半是旧版本误改留下的（对方身上被写进了 `MADMODEL_API_KEY`，地址又不是清华站点）。
        只提醒、不自动改：原来的值可能要用户自己定，程序不该猜。
        """

        for key, entry in providers.items():
            if not isinstance(entry, dict) or str(key) == ours_key:
                continue
            if entry.get("apiKeyEnv") != constants.DSH_CREDENTIAL_KEY:
                continue
            if cls._is_madmodel_url(str(entry.get("baseURL") or "")):
                continue
            message = (
                f"{profile_name}：provider {key} 带着 MADMODEL_API_KEY 标记，但地址不是清华站点、"
                "键名也不是 madmodel。本程序不会再动它；如果它是你自己的其它服务，"
                "请检查它的 apiKeyEnv / models / reasoning 有没有被旧版本改过"
                "（可以用主界面「一键还原」从备份恢复）。"
            )
            logger.warning(message)
            result.note("注意：" + message)

    @classmethod
    def _is_madmodel_url(cls, value: str) -> bool:
        return cls._url_host(value) == constants.MADMODEL_HOST

    @classmethod
    def _is_local_forward(cls, value: str) -> bool:
        """本地转发：http://127.0.0.1:xxxx/v1（DSH 里常见的接法）。"""

        parts = urlsplit(value)
        if (parts.hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
            return False
        return parts.path.startswith("/v1")

    @classmethod
    def _find_provider(cls, providers: dict) -> tuple[str, dict] | None:
        """只认**看得出来的**我们自己，其它一律不碰（宁可新建也不猜）。

        只有两种情况算我们的 provider：
        1. `baseURL` 的域名就是清华站点（`madmodel.cs.tsinghua.edu.cn`）——认得出就是它；
        2. provider 的键名就叫 `madmodel`——这个名字是我们约定的，且 `apiKeyEnv` 要么空、
           要么就是我们的标记（键被别人的配置占着时会走"被占用"的报错分支，不覆盖）。

        以前还有一条"只要是本地转发 `127.0.0.1:*/v1` 就算我们的"：这会误伤用户自己的
        `ollama`、`isearcher` 这类本地 provider（改掉它们的 `apiKeyEnv`、模型列表，
        还会把 provider 级的 `reasoning` 写进去，导致 DSH 报
        `does not support reasoning effort ...`）。而且一旦误改过一次、对方身上就留了
        `apiKeyEnv: MADMODEL_API_KEY`，后续同步会继续把它当自己人——所以这条规则整条删掉。
        """

        named = providers.get(constants.DSH_PROVIDER_KEY)
        if isinstance(named, dict):
            env = named.get("apiKeyEnv")
            base = str(named.get("baseURL") or "")
            # 键名是我们约定的，但还要看它像不像我们的（占着坑的别人的配置要走"被占用"报错分支）
            looks_ours = (
                env == constants.DSH_CREDENTIAL_KEY
                or cls._is_madmodel_url(base)
                or (cls._is_local_forward(base) and env in (None, ""))
            )
            if looks_ours:
                return str(constants.DSH_PROVIDER_KEY), named

        for key, entry in providers.items():
            if not isinstance(entry, dict) or str(key) == constants.DSH_PROVIDER_KEY:
                continue
            if cls._is_madmodel_url(str(entry.get("baseURL") or "")):
                return str(key), entry
        return None

    # —— 只换我们自己的那两块文本，其余原文照抄 ——

    @staticmethod
    def _replace_ref_line(text: str, key: str, value: str) -> str | None:
        """把 `KEY: 旧值` 这一行换成新值，文件其余部分（含别的 refs 与排版）原样保留。

        找不到这一行、或者原值是多行标量（`|` / `>`）时返回 None，调用方退回整份重写。
        """

        pattern = re.compile(
            rf"^(?P<indent>\s*){re.escape(key)}\s*:\s*"
            r"(?P<quote>[\"']?)(?P<old>[^\s#]*)(?P=quote)(?P<tail>\s*(?:#.*)?)$"
        )
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            match = pattern.match(body)
            if not match:
                continue
            old = match.group("old") or ""
            if old.startswith("|") or old.startswith(">"):
                return None  # 多行标量：别乱动，交给整份重写 + 回读校验
            quote = match.group("quote") or ""
            newline = line[len(body) :]
            lines[index] = (
                f"{match.group('indent')}{key}: {quote}{value}{quote}{match.group('tail')}{newline}"
            )
            return "".join(lines)
        return None

    @staticmethod
    def _provider_span(lines: list[str], provider_key: str) -> tuple[int, int] | None:
        """找出 `providers:` 里某个 provider 的行范围 [start, end)。"""

        providers_indent: int | None = None
        key_indent: int | None = None
        start: int | None = None
        key_pattern = re.compile(rf"^\s*['\"]?{re.escape(provider_key)}['\"]?\s*:\s*(#.*)?$")
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if providers_indent is None:
                if re.match(r"^\s*providers\s*:\s*(#.*)?$", line):
                    providers_indent = indent
                continue
            if start is None:
                if indent > providers_indent and key_pattern.match(line):
                    start = index
                    key_indent = indent
                continue
            if indent <= (key_indent or 0):
                return start, index
        return (start, len(lines)) if start is not None else None

    @staticmethod
    def _providers_block_end(
        lines: list[str], start: int = 0, end: int | None = None
    ) -> int | None:
        """`providers:` 块最后一个子项之后的行号（新 provider 插到这里）。

        只在"块式写法"上有效；`providers: {…}` 这种内联写法返回 None，
        调用方会退回整份重写（没有别人时）或直接报错（有别人时）。
        `start` / `end` 限定在某个条目范围内找 —— 同一文件里别的条目也可能有 providers。
        """

        providers_indent: int | None = None
        last_child: int | None = None
        limit = len(lines) if end is None else min(end, len(lines))
        for index in range(max(0, start), limit):
            line = lines[index]
            stripped = line.strip()
            if providers_indent is None:
                if stripped and not stripped.startswith("#") and re.match(
                    r"^\s*providers\s*:\s*(#.*)?$", line
                ):
                    providers_indent = len(line) - len(line.lstrip(" "))
                continue
            if not stripped or stripped.startswith("#"):
                continue
            if (len(line) - len(line.lstrip(" "))) <= providers_indent:
                break
            last_child = index
        if providers_indent is None or last_child is None:
            return None
        return last_child + 1

    @staticmethod
    def _entry_span(
        lines: list[str], entry_id: str, entry_name: str | None = None
    ) -> tuple[int, int] | None:
        """找出顶层数组里某个条目的行范围 [start, end)。

        按"整块文本"匹配 id / name：`- id: xxx`、id 带引号、`- name:` 写在前面
        这些写法都能认出来。尾部紧跟着的注释/空行不算在这个条目的替换范围里，
        换块的时候会原样留在文件里。
        """

        starts: list[int] = []
        indents: list[int] = []
        for line in lines:
            match = re.match(r"^(\s*)-\s+\S", line)
            if match:
                indents.append(len(match.group(1)))
        if not indents:
            return None
        base_indent = min(indents)
        for index, line in enumerate(lines):
            match = re.match(r"^(\s*)-\s+\S", line)
            if match and len(match.group(1)) == base_indent:
                starts.append(index)
        if not starts:
            return None

        id_re = re.compile(
            rf"^\s*(?:-\s+)?id\s*:\s*['\"]?{re.escape(entry_id)}['\"]?\s*(?:#.*)?$",
            re.MULTILINE,
        )
        name_re = (
            re.compile(
                rf"^\s*(?:-\s+)?name\s*:\s*['\"]?{re.escape(entry_name)}['\"]?\s*(?:#.*)?$",
                re.MULTILINE,
            )
            if entry_name
            else None
        )
        bounds = [*starts, len(lines)]
        for position in range(len(starts)):
            start, end = bounds[position], bounds[position + 1]
            block = "".join(lines[start:end])
            if id_re.search(block) or (name_re is not None and name_re.search(block)):
                # 尾部的空行/注释不属于这个条目，替换时留在原位
                while end > start + 1 and (
                    not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")
                ):
                    end -= 1
                return start, end
        return None

    @classmethod
    def _keep_other_blocks_verbatim(
        cls,
        original: str,
        new_text: str,
        *,
        provider_key: str,
        provider_changed: bool,
        entry_changed: bool,
        provider_created: bool = False,
        entry_existed: bool = True,
        allow_full_rewrite: bool = True,
    ) -> str:
        """把新文本里"我们改动的那几块"贴回原文件，别的地方一个字节都不动。

        整份 dump 虽然是合法的，但 ruamel 会把别人 provider 里的流式列表空格、注释对齐
        这类排版重排一遍 —— 那也算改了用户的配置。这里只允许三种最小改动：
        · 我们的 provider 块（字段真的有变化时，整块替换）；
        · 新 provider（原文里还没有 madmodel 时，插到 `providers:` 块的末尾）；
        · `agent-default-model` 条目（被我们改了时整块替换；原文里没有才追加到末尾）。

        定位失败时的口径：
        · 同一文件里没有别的 provider（`allow_full_rewrite=True`）→ 退回整份重写：
          此时不可能碰到别人的配置，宁可排版动一点也不写错内容；
        · 有别的 provider → 直接报错、这次不写：绝不为了写对内容而重排别人的配置。
        """

        def cannot_locate(reason: str) -> str:
            if allow_full_rewrite:
                logger.info("%s，这次退回整份重写", reason)
                return new_text
            raise ValueError(f"{reason}；同一文件里还有其它 provider，为不动它们这次没有写入")

        orig_lines = original.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        # (起始行, 结束行, 新内容)；插入是起止相同的空区间。按行号从后往前应用。
        edits: list[tuple[int, int, list[str]]] = []
        appends: list[str] = []

        if provider_changed:
            new_span = cls._provider_span(new_lines, provider_key)
            if new_span is None:
                return cannot_locate(f"新文本里定位不到 provider {provider_key} 的块")
            orig_span = cls._provider_span(orig_lines, provider_key)
            if orig_span is not None:
                edits.append((orig_span[0], orig_span[1], new_lines[new_span[0] : new_span[1]]))
            elif provider_created:
                # 只能插到**我们这个条目**（llm-pi-ai）的 providers 块里：
                # 同一文件里别的条目也可能有自己的 providers，插错地方就动了别人的配置
                llm_span = cls._entry_span(
                    orig_lines, constants.DSH_LLM_ENTRY_ID, constants.DSH_LLM_ENTRY_NAME
                )
                insert_at = (
                    cls._providers_block_end(orig_lines, llm_span[0], llm_span[1])
                    if llm_span is not None
                    else None
                )
                if insert_at is None:
                    return cannot_locate(
                        f"原文里的 providers 不是块式写法，没法把 provider {provider_key} 插进去"
                    )
                edits.append((insert_at, insert_at, new_lines[new_span[0] : new_span[1]]))
            else:
                # 数据层找到了它、文本层却定位不到（例如写成了内联 map）：
                # 宁可报错也不整份重写，否则会连带重排同一文件里别人的 provider
                return cannot_locate(f"原文里定位不到 provider {provider_key} 的文本块")

        if entry_changed:
            new_span = cls._entry_span(
                new_lines,
                constants.DSH_DEFAULT_MODEL_ENTRY_ID,
                constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
            )
            if new_span is None:
                return cannot_locate("新文本里定位不到 agent-default-model 条目")
            orig_span = cls._entry_span(
                orig_lines,
                constants.DSH_DEFAULT_MODEL_ENTRY_ID,
                constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
            )
            if orig_span is not None:
                edits.append((orig_span[0], orig_span[1], new_lines[new_span[0] : new_span[1]]))
            elif not entry_existed:
                # 原来没有这个条目（我们新建的）：整块追加到文件末尾
                appends.extend(new_lines[new_span[0] : new_span[1]])
            else:
                # 数据层确认它原本存在：append 会写出两条同名条目，只能报错/退回整份重写
                return cannot_locate("原文里定位不到 agent-default-model 条目")

        if not edits and not appends:
            return new_text

        result = list(orig_lines)

        def edit_order(item: tuple[int, int, list[str]]) -> tuple[int, int]:
            """从后往前应用；行号相同时**替换先做、插入后做**。

            否则"在 providers 块末尾插入新 provider"会和"替换紧跟在后面的
            agent-default-model 条目"撞在同一个起点上，替换会把插入块的开头吃掉。
            """

            start, end, _chunk = item
            return (start, 0 if start == end else 1)

        for start, end, chunk in sorted(edits, key=edit_order, reverse=True):
            result[start:end] = chunk
        if appends:
            if result and not result[-1].endswith("\n"):
                result[-1] += "\n"
            if result and result[-1].strip():
                result.append("\n")
            result.extend(appends)
        return "".join(result)

    @staticmethod
    def _merge_models(existing: object, fetched: list[dict]) -> list[dict]:
        """校正模型列表。

        规则：
        - id 看起来不像模型 id（带空格/括号等，多半是字段填串了）的条目直接丢掉；
        - 我们维护的官方模型（DEFAULT_MODELS 里的 id）一律以清单为准，
          这样被 DSH 界面改花的名字、丢掉的思考档能在下一次同步时自己修回来；
        - 其它 id 的条目原样保留（用户自己加的模型不动）；
        - 站点能读到模型列表时，以站点列表为准，但内容仍优先用清单里的完整条目。
        """

        # deepcopy：DEFAULT_MODELS 是模块级常量，浅拷贝会让嵌套的 input / reasoningEfforts
        # 和常量共享对象，将来谁改一下就会污染全局。
        canonical = {str(model["id"]): copy.deepcopy(model) for model in constants.DEFAULT_MODELS}
        base: list[dict] = []
        if isinstance(existing, list):
            for item in existing:
                if (
                    isinstance(item, dict)
                    and item.get("id")
                    and MODEL_ID_PATTERN.match(str(item["id"]))
                ):
                    model_id = str(item["id"])
                    if model_id in canonical:
                        base.append(copy.deepcopy(canonical[model_id]))
                        continue
                    entry = copy.deepcopy(item)
                    name = str(entry.get("name") or "").strip()
                    if not name or name == constants.PROVIDER_DISPLAY_NAME:
                        entry["name"] = f"{entry['id']} (THU)"
                    base.append(entry)
        if not base:
            base = [copy.deepcopy(model) for model in constants.DEFAULT_MODELS]

        if not fetched:
            return base

        fetched_ids = [str(item.get("id")) for item in fetched if item.get("id")]
        fetched_by_id = {str(item["id"]): dict(item) for item in fetched if item.get("id")}
        by_id = {str(item.get("id")): item for item in base}
        merged: list[dict] = []
        for model_id in fetched_ids:
            merged.append(
                by_id.get(model_id)
                or canonical.get(model_id)
                or fetched_by_id.get(model_id)
                or {
                    "id": model_id,
                    "name": f"{model_id} (THU)",
                    "contextWindow": constants.CONTEXT_WINDOW,
                    "input": ["text", "image"],
                }
            )
        return merged or base

    def _repair_default_model(self, data: list, models: list[dict], provider_key: str) -> bool:
        """默认模型指向 madmodel 时，保证它的 model 是真实存在的模型 id。

        用户在 DSH 界面里换过 provider 又没换 model 时，会留下
        provider=madmodel + model=<另一个 provider 的 id> 这种指不到东西的组合。
        这里只修"provider 已经是 madmodel"的条目，不动用户选的是别的 provider 的情况。
        """

        entry = self._find_entry(
            data,
            constants.DSH_DEFAULT_MODEL_ENTRY_ID,
            constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
        )
        if entry is None:
            return False
        config = entry.get("config")
        if not isinstance(config, dict) or config.get("provider") != provider_key:
            return False
        model_ids = [str(item["id"]) for item in models if item.get("id")]
        before = copy.deepcopy(config)
        if model_ids and config.get("model") not in model_ids:
            config["model"] = self._preferred_model_id(model_ids)
        self._ensure_default_effort(config, models)
        return config != before

    # —— 思考档（reasoning effort）——

    @staticmethod
    def _supported_efforts(model: dict | None) -> list[str]:
        """这个模型声明了哪些思考档。"""

        efforts = (model or {}).get("reasoningEfforts")
        if not isinstance(efforts, dict):
            return []
        return [str(level) for level in efforts.keys() if str(level) in constants.THINKING_LEVELS]

    @classmethod
    def _pick_effort(cls, supported: list[str]) -> str:
        """从支持的档位里挑一个当默认：思考得够用，但不顶到最贵。"""

        for level in ("high", "max", "medium", "low", "xhigh", "minimal"):
            if level in supported:
                return level
        return supported[0] if supported else ""

    @classmethod
    def _safe_provider_reasoning(cls, models: list[dict]) -> str:
        """provider 级默认真要写时，只能写所有模型都支持的档位。"""

        sets = [set(cls._supported_efforts(model)) for model in models]
        sets = [item for item in sets if item]
        if not sets:
            return ""
        common = set.intersection(*sets)
        if not common:
            return ""
        for level in ("medium", "high", "low", "max", "xhigh", "minimal"):
            if level in common:
                return level
        return sorted(common)[0]

    @classmethod
    def _reasoning_ok_for_all(cls, level: str, models: list[dict]) -> bool:
        """这个档位是不是所有声明了思考档的模型都支持。

        没有模型声明档位时无从判断，按"合法"处理——**宁可不动用户的值**。
        """

        declared = [set(cls._supported_efforts(model)) for model in models]
        declared = [item for item in declared if item]
        if not declared:
            return True
        return all(level in item for item in declared)

    def _ensure_default_effort(self, config: dict, models: list[dict]) -> None:
        """默认模型的思考档必须是该模型支持的——不支持就换一个支持的。

        这正是"第一次运行就报 doesn't support reasoning effort medium"的根因：
        没有显式档位时会回落到 provider 的 reasoning，而 DeepSeek-V4.1-Flash 只支持 low/high/max。
        """

        model_id = str(config.get("model") or "")
        model = next((item for item in models if str(item.get("id")) == model_id), None)
        supported = self._supported_efforts(model)
        if not supported:
            return  # 该模型没有思考档，别硬写
        if config.get("reasoningEffort") in supported:
            return
        config["reasoningEffort"] = self._pick_effort(supported)

    def _preferred_model_id(self, model_ids: list[str]) -> str:
        """设置里指定了默认模型就用它，否则用清单里的第一个。"""

        if self.default_model_id and self.default_model_id in model_ids:
            return self.default_model_id
        return model_ids[0] if model_ids else ""

    def _point_default_model(
        self, data: list, models: list[dict], provider_key: str, result  # noqa: ANN001 - ApplyResult
    ) -> bool:
        entry = self._find_entry(
            data,
            constants.DSH_DEFAULT_MODEL_ENTRY_ID,
            constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
        )
        if entry is None:
            entry = {
                "id": constants.DSH_DEFAULT_MODEL_ENTRY_ID,
                "name": constants.DSH_DEFAULT_MODEL_ENTRY_NAME,
                "config": {},
            }
            data.append(entry)
        config = entry.get("config")
        if not isinstance(config, dict):
            config = {}
            entry["config"] = config
        before = copy.deepcopy(config)
        previous = config.get("provider")
        config["provider"] = provider_key
        if previous and previous != provider_key:
            # 这是"统一所有 profile"这个开关的语义，写清楚点免得用户以为是 bug
            result.note(
                f"按「统一所有 profile 到 madmodel」把默认模型从 {previous} 切到 {provider_key}"
                "（不想要就在设置里取消勾选）"
            )
        model_ids = [str(item["id"]) for item in models if item.get("id")]
        if model_ids and config.get("model") not in model_ids:
            # 换 provider 时必须一起换 model，否则会留下"指着 madmodel 却填别的模型 id"的组合
            config["model"] = self._preferred_model_id(model_ids)
        self._ensure_default_effort(config, models)
        return config != before

