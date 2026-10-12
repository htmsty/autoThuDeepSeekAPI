"""DeepSeek Harness（DSH）配置写入。"""

from __future__ import annotations

import logging
from pathlib import Path

from ruamel.yaml import YAML

from .. import constants
from ..backup import BackupManager
from ..paths import TargetPaths
from .base import ApplyResult, TargetStatus

logger = logging.getLogger(__name__)


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
        data = yaml.load(text)
        if not isinstance(data, dict):
            raise ValueError("凭据文件结构不是映射（mapping）")

        refs = data.get("refs")
        if refs is None:
            data["refs"] = {}
            refs = data["refs"]
        if not isinstance(refs, dict):
            raise ValueError("凭据文件的 refs 不是映射")

        if refs.get(constants.DSH_CREDENTIAL_KEY) == token:
            result.note("凭据里的 MADMODEL_API_KEY 已是该 Token，无需改动")
            return False

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
        return constants.MADMODEL_HOST in text or f"{constants.DSH_PROVIDER_KEY}:" in text

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

        provider = providers.get(constants.DSH_PROVIDER_KEY)
        if not isinstance(provider, dict):
            provider = {}
            providers[constants.DSH_PROVIDER_KEY] = provider

        provider["displayName"] = constants.PROVIDER_DISPLAY_NAME
        provider["apiKeyEnv"] = constants.DSH_CREDENTIAL_KEY
        provider.setdefault("api", "openai-completions")
        provider.setdefault("reasoning", "medium")
        provider["baseURL"] = constants.MADMODEL_API_BASE

        merged = self._merge_models(provider.get("models"), models)
        if merged:
            provider["models"] = merged

        if self.unify_all_profiles:
            self._point_default_model(data, merged)

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

    @staticmethod
    def _merge_models(existing: object, fetched: list[dict]) -> list[dict]:
        """以现有条目为底，用站点读到的模型 id 校正列表；空列表时退回默认。"""

        base: list[dict] = []
        if isinstance(existing, list):
            for item in existing:
                if isinstance(item, dict) and item.get("id"):
                    base.append(dict(item))
        if not base:
            base = [dict(model) for model in constants.DEFAULT_MODELS]

        if not fetched:
            return base

        fetched_ids = [str(item.get("id")) for item in fetched if item.get("id")]
        by_id = {str(item.get("id")): item for item in base}
        merged: list[dict] = []
        for model_id in fetched_ids:
            merged.append(by_id.get(model_id, {"id": model_id, "name": model_id}))
        return merged or base

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
        if models:
            config.setdefault("model", models[0].get("id"))

