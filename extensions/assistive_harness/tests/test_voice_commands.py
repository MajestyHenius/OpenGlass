import tempfile
import unittest
from pathlib import Path

import yaml

from extensions.assistive_harness.registry import SkillRegistry, RegistryError
from extensions.assistive_harness.router import RuleIntentRouter
from extensions.assistive_harness.schemas import ControlIntent


class VoiceCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        original = Path(__file__).resolve().parents[1] / "config" / "skills.example.yaml"
        raw = yaml.safe_load(original.read_text(encoding="utf-8"))
        for spec in raw["skills"].values():
            spec["prompt_file"] = str((original.parent / spec["prompt_file"]).resolve())
        example = (original.parent / raw["voice_commands_file"]).resolve()
        self.example = root / "voice_commands.example.yaml"
        self.example.write_bytes(example.read_bytes())
        self.local = root / "voice_commands.local.yaml"
        raw["voice_commands_file"] = self.example.name
        self.config = root / "skills.yaml"
        self.config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    def test_local_partial_override_and_empty_list(self):
        self.local.write_text("控制指令:\n  暂停:\n    短句: [请安静一会]\n    句中关键词: []\n", encoding="utf-8")
        registry = SkillRegistry(self.config)
        router = RuleIntentRouter(registry)
        self.assertEqual(router.route("请安静一会").intent, ControlIntent.STOP_SPEECH)
        self.assertEqual(router.route("现在停一下然后聊聊").intent, ControlIntent.NONE)
        self.assertEqual(router.route("重新开始").intent, ControlIntent.RESET_SESSION)
        self.assertEqual(router.route("帮我找手机").slots, {"target": "手机"})
        self.assertEqual(registry.voice_commands_path, self.local)

    def test_invalid_phrase_type_fails_clearly(self):
        self.local.write_text("控制指令:\n  暂停:\n    短句: 暂停\n", encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "list of phrases"):
            SkillRegistry(self.config)

    def test_find_pattern_requires_target(self):
        self.local.write_text("技能指令:\n  找物:\n    匹配句式: ['找(.+)']\n", encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "target group"):
            SkillRegistry(self.config)

    def test_old_custom_registry_without_overlay_remains_supported(self):
        raw = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        raw.pop("voice_commands_file")
        raw["control"]["stop_speech"] = {"phrases": ["休息一下"]}
        self.config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        self.assertEqual(RuleIntentRouter(SkillRegistry(self.config)).route("休息一下").intent,
                         ControlIntent.STOP_SPEECH)
