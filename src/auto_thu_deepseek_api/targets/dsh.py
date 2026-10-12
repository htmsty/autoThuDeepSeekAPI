"""DeepSeek Harness（DSH）配置写入。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from ruamel.yaml import YAML

from .. import constants
from ..backup import BackupManager
from ..paths import TargetPaths
from .base import ApplyResult, TargetStatus

logger = logging.getLogger(__name__)

# 站点的模型 id 长这样：DeepSeek-V4.1-Flash、qwen3.8-27b。
# 出现空格、括号、逗号这类字符多半是字段被填串了（显示名填进了 id），要修掉。
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$")


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    return yaml


class DshTarget:
    key = "dsh"
    label = "DeepSeek Harness (DSH)"

    def __init__(self, paths: TargetPaths, unify_all_profiles: bool = False) -> None:
        self.paths = paths
        self.unify_all_profiles = unify_all_profiles

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

        self._write_profiles(token, models, backup, result)
        return result

    def _write_credentials(
        self,
        path: Path,
        token: str,
        backup: BackupManager,
        result: ApplyResult,
    ) -> bool:
        yaml = _yaml()
        text = path.read_text(encoding="utf-8")
        rebuilt = False
        try:
            data = yaml.load(text)
        except Exception as exc:  # noqa: BLE001 - 文件已经坏了，下面按行救回能救的
            result.note(f"凭据文件解析失败（{type(exc).__name__}），已留底并尝试重建")
            backup.backup_file(path, "dsh-credentials-broken")
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
            result.note("凭据里的 MADMODEL_API_KEY 已是该 Token，无需改动")
            return False

        if not rebuilt:
            backup.backup_file(path, "dsh-credentials")
        refs[constants.DSH_CREDENTIAL_KEY] = token

        import io

        stream = io.StringIO()
        yaml.dump(data, stream)
        new_text = stream.getvalue()

        # 回读校验：确认写进去的就是这个 Token（避免折行标量之类的坑）
        from ruamel.yaml import YAML as CheckYAML

        checker = CheckYAML(typ="safe")
        parsed = checker.load(new_text)
        if not isinstance(parsed, dict) or parsed.get("refs", {}).get(constants.DSH_CREDENTIAL_KEY) != token:
            raise ValueError("回读校验失败：写入后解析出的 Token 与预期不一致")

        path.write_text(new_text, encoding="utf-8")
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
        # 指向清华站点、已经叫 madmodel、或者带着我们的凭据引用（本地转发的情形）都算在范围内
        return (
            constants.MADMODEL_HOST in text
            or f"{constants.DSH_PROVIDER_KEY}:" in text
            or constants.DSH_CREDENTIAL_KEY in text
        )

    def _write_profiles(
        self,
        token: str,
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
        yaml = _yaml()
        original = path.read_text(encoding="utf-8")
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

        if found is None:
            if constants.DSH_PROVIDER_KEY in providers:
                result.fail(
                    f"{name}：providers.madmodel 已被其它配置占用"
                    "（它的 apiKeyEnv 与 baseURL 都不指向清华站点），按约定不覆盖它"
                )
                return False
            provider: dict = {}
            providers[constants.DSH_PROVIDER_KEY] = provider
            provider["displayName"] = constants.PROVIDER_DISPLAY_NAME
            provider["api"] = "openai-completions"
            provider["reasoning"] = "medium"
            provider["baseURL"] = constants.MADMODEL_API_BASE
            result.note(f"{name}：已新建 provider madmodel")
        else:
            key, provider = found
            result.note(f"{name}：已找到 provider {key}")

        # 只合并我们负责的字段：apiKeyEnv 与 models。其余字段只在缺失时补上，
        # 不覆盖用户在 DSH 里自己对名称/地址/思考档的调整。
        provider["apiKeyEnv"] = constants.DSH_CREDENTIAL_KEY
        if not provider.get("reasoning"):
            provider["reasoning"] = "medium"
        if not provider.get("api"):
            provider["api"] = "openai-completions"
        if not provider.get("baseURL"):
            provider["baseURL"] = constants.MADMODEL_API_BASE

        merged = self._merge_models(provider.get("models"), models)
        if merged:
            provider["models"] = merged

        # 名称是空、或被写成了某个模型 id（DSH 界面把字段填串的典型样子）才纠正，
        # 用户自己起的名字不动。
        display_name = str(provider.get("displayName") or "").strip()
        model_ids = {str(model.get("id")) for model in (merged or [])}
        if not display_name or display_name in model_ids:
            provider["displayName"] = constants.PROVIDER_DISPLAY_NAME

        if self.unify_all_profiles:
            self._point_default_model(data, merged)
        else:
            self._repair_default_model(data, merged)

        import io

        stream = io.StringIO()
        yaml.dump(data, stream)
        new_text = stream.getvalue()
        if new_text == original:
            result.note(f"{path.parent.name}：供应商配置已是最新")
            return False

        backup.backup_file(path, "dsh-profile")
        path.write_text(new_text, encoding="utf-8")
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
        """先按 apiKeyEnv 认领，再按 baseURL 指向清华站点（或本地转发）认领。"""

        by_env: list[tuple[str, dict]] = []
        by_url: list[tuple[str, dict]] = []
        for key, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            base = str(entry.get("baseURL") or "")
            if entry.get("apiKeyEnv") == constants.DSH_CREDENTIAL_KEY:
                by_env.append((str(key), entry))
            elif cls._is_madmodel_url(base) or cls._is_local_forward(base):
                by_url.append((str(key), entry))
        for key, entry in by_env:
            if cls._is_madmodel_url(str(entry.get("baseURL") or "")):
                return key, entry
        if by_env:
            return by_env[0]
        return by_url[0] if by_url else None

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

        canonical = {str(model["id"]): dict(model) for model in constants.DEFAULT_MODELS}
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
                        base.append(dict(canonical[model_id]))
                        continue
                    entry = dict(item)
                    name = str(entry.get("name") or "").strip()
                    if not name or name == constants.PROVIDER_DISPLAY_NAME:
                        entry["name"] = f"{entry['id']} (THU)"
                    base.append(entry)
        if not base:
            base = [dict(model) for model in constants.DEFAULT_MODELS]

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

    def _repair_default_model(self, data: list, models: list[dict]) -> None:
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
            return
        config = entry.get("config")
        if not isinstance(config, dict) or config.get("provider") != constants.DSH_PROVIDER_KEY:
            return
        model_ids = [str(item["id"]) for item in models if item.get("id")]
        if model_ids and config.get("model") not in model_ids:
            config["model"] = model_ids[0]

    def _point_default_model(self, data: list, models: list[dict]) -> None:
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
        config["provider"] = constants.DSH_PROVIDER_KEY
        model_ids = [str(item["id"]) for item in models if item.get("id")]
        if model_ids and config.get("model") not in model_ids:
            # 换 provider 时必须一起换 model，否则会留下"指着 madmodel 却填别的模型 id"的组合
            config["model"] = model_ids[0]

