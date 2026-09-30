from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class RegistryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    skill_id: str
    text: str
    path: str
    sha256: str
    slots: dict[str, Any]


class SkillRegistry:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path).resolve()
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        self.version = int(raw.get("version", 1))
        self.default_skill = str(raw.get("default_skill", "idle_chat"))
        self.control = dict(raw.get("control") or {})
        self.skills: dict[str, dict[str, Any]] = dict(raw.get("skills") or {})
        self.voice_commands_path: Path | None = None
        if raw.get("voice_commands_file"):
            example = (self.config_path.parent / str(raw["voice_commands_file"])).resolve()
            self._load_voice_commands(example)
            local = example.with_name(example.name.replace(".example.yaml", ".local.yaml"))
            if local != example and local.is_file():
                self._load_voice_commands(local)
        if self.default_skill not in self.skills:
            raise RegistryError(f"default skill is missing: {self.default_skill}")
        self._validate_prompt_files()

    def _load_voice_commands(self, path: Path) -> None:
        controls = {"暂停": "stop_speech", "恢复": "resume_speech",
                    "重新开始": "reset_session", "回到聊天": "return_to_chat",
                    "取消任务": "cancel_skill"}
        skills = {"普通聊天": "idle_chat", "找物": "find_object",
                  "读文字": "read_text", "描述场景": "describe_scene",
                  "室内领路": "obstacle_avoidance"}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise RegistryError(f"cannot load voice commands: {path}: {exc}") from exc
        if not isinstance(raw, dict) or set(raw) - {"控制指令", "技能指令"}:
            raise RegistryError(f"invalid voice command sections: {path}")
        for section, names, target, fields in (
            ("控制指令", controls, self.control,
             {"短句": "phrases", "句中关键词": "embedded_phrases"}),
            ("技能指令", skills, self.skills,
             {"触发短句": "activation_phrases", "匹配句式": "activation_patterns"}),
        ):
            entries = raw.get(section, {})
            if not isinstance(entries, dict) or set(entries) - set(names):
                raise RegistryError(f"invalid {section} names: {path}")
            for name, settings in entries.items():
                if not isinstance(settings, dict) or set(settings) - set(fields):
                    raise RegistryError(f"invalid voice command fields: {path}: {name}")
                if section == "控制指令":
                    allowed = ({"句中关键词"} if name == "恢复" else
                               {"短句"} if name in {"回到聊天", "取消任务"} else set(fields))
                    if set(settings) - allowed:
                        raise RegistryError(f"unsupported match mode: {path}: {name}")
                for field, values in settings.items():
                    if not isinstance(values, list) or any(
                        not isinstance(v, str) or not v.strip() for v in values
                    ):
                        raise RegistryError(f"expected a list of phrases: {path}: {name}/{field}")
                    if field == "匹配句式":
                        for pattern in values:
                            try:
                                compiled = re.compile(pattern)
                            except re.error as exc:
                                raise RegistryError(f"invalid pattern: {path}: {name}: {exc}") from exc
                            if name == "找物" and "target" not in compiled.groupindex:
                                raise RegistryError(f"find-object pattern requires target group: {path}")
                    target.setdefault(names[name], {})[fields[field]] = list(values)
        self.voice_commands_path = path

    def _prompt_path(self, skill_id: str) -> Path:
        spec = self.get(skill_id)
        path = Path(str(spec.get("prompt_file") or ""))
        if not path.is_absolute():
            path = self.config_path.parent / path
        return path.resolve()

    def prompt_path(self, skill_id: str) -> Path:
        """Return the user-editable prompt path exposed by the registry."""
        return self._prompt_path(skill_id)

    def _validate_prompt_files(self) -> None:
        for skill_id, spec in self.skills.items():
            if not isinstance(spec, dict):
                raise RegistryError(f"invalid skill spec: {skill_id}")
            path = self._prompt_path(skill_id)
            if not path.is_file():
                raise RegistryError(f"prompt file is missing for {skill_id}: {path}")

    def get(self, skill_id: str) -> dict[str, Any]:
        spec = self.skills.get(skill_id)
        if spec is None:
            raise RegistryError(f"unknown skill: {skill_id}")
        if not isinstance(spec, dict):
            raise RegistryError(f"invalid skill spec: {skill_id}")
        return spec

    def is_enabled(self, skill_id: str) -> bool:
        return bool(self.get(skill_id).get("enabled", False))

    def cooldown_ms(self, skill_id: str) -> int:
        return int(self.get(skill_id).get("cooldown_ms", 0))

    def task_trigger(
        self, skill_id: str, slots: dict[str, Any] | None = None
    ) -> str:
        """Render the optional one-shot command sent to a fresh Skill Session."""
        text = str(self.get(skill_id).get("task_trigger") or "").strip()
        slots = dict(slots or {})
        variables = set(re.findall(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}", text))
        missing = sorted(name for name in variables if not str(slots.get(name, "")).strip())
        if missing:
            raise RegistryError(f"missing task trigger variables for {skill_id}: {missing}")
        for name in variables:
            text = re.sub(
                r"\{\{\s*" + re.escape(name) + r"\s*\}\}",
                str(slots[name]),
                text,
            )
        return text

    def render(self, skill_id: str, slots: dict[str, Any] | None = None) -> RenderedPrompt:
        if not self.is_enabled(skill_id):
            raise RegistryError(f"skill is disabled: {skill_id}")
        slots = dict(slots or {})
        spec = self.get(skill_id)
        schema = dict(spec.get("slot_schema") or {})
        for name, slot_spec in schema.items():
            if bool((slot_spec or {}).get("required")) and not str(slots.get(name, "")).strip():
                raise RegistryError(f"missing required slot '{name}' for {skill_id}")

        path = self._prompt_path(skill_id)
        text = path.read_text(encoding="utf-8")
        variables = set(re.findall(r"\{\{\s*([a-zA-Z_][\w]*)\s*\}\}", text))
        missing = sorted(name for name in variables if name not in slots)
        if missing:
            raise RegistryError(f"missing prompt variables for {skill_id}: {missing}")
        for name in variables:
            text = re.sub(
                r"\{\{\s*" + re.escape(name) + r"\s*\}\}",
                str(slots[name]),
                text,
            )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return RenderedPrompt(
            skill_id=skill_id,
            text=text,
            path=str(path),
            sha256=digest,
            slots=slots,
        )
