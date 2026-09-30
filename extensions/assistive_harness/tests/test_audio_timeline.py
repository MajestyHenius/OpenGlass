import unittest
import numpy as np

from extensions.assistive_harness.asr.energy_vad import EnergyVAD
from extensions.assistive_harness.echo_guard import EchoGuard
from extensions.assistive_harness.schemas import ControlIntent
from extensions.assistive_harness.phase_b.esp32_runtime import DeviceAudioClock


class AudioTimelineTests(unittest.TestCase):
    def test_network_stall_is_not_silence(self):
        vad = EnergyVAD(min_speech_ms=100, end_silence_ms=300)
        speech = np.full(1600, .1, np.float32)
        silence = np.zeros(1600, np.float32)
        self.assertIsNone(vad.feed(speech, 0))
        self.assertIsNone(vad.feed(speech, 9000))
        self.assertIsNone(vad.feed(silence, 15000))
        self.assertIsNone(vad.feed(silence, 15000))
        result = vad.feed(silence, 15000)
        self.assertEqual(result.audio.size, 8000)

    def test_burst_still_obeys_sample_duration_limit(self):
        vad = EnergyVAD(min_speech_ms=100, max_utterance_ms=300)
        speech = np.full(1600, .1, np.float32)
        self.assertIsNone(vad.feed(speech, 0))
        self.assertIsNone(vad.feed(speech, 0))
        self.assertEqual(vad.feed(speech, 0).audio.size, 4800)

    def test_device_time_survives_delayed_delivery_and_packet_loss(self):
        clock = DeviceAudioClock()
        self.assertEqual(clock.map_packet(1, 100, 320, 1000), (980, True))
        self.assertEqual(clock.map_packet(2, 120, 320, 9000), (1000, False))
        self.assertEqual(clock.map_packet(4, 160, 320, 9000), (1040, True))

    def test_device_time_wrap_and_restart(self):
        clock = DeviceAudioClock()
        clock.map_packet(0xffffffff, 0xfffffff0, 320, 1000)
        self.assertEqual(clock.map_packet(0, 4, 320, 1010), (1000, False))
        self.assertEqual(clock.map_packet(0, 0, 320, 2000), (1980, True))

    def test_short_model_fragment_does_not_swallow_real_command(self):
        guard = EchoGuard()
        guard.note_model_text("色笔记本。", at_ms=1000)
        self.assertTrue(guard.evaluate("还黑色笔记本帮我找一下手机在在哪里",
                                      ControlIntent.ACTIVATE_SKILL, at_ms=1100).allow)
        self.assertFalse(guard.evaluate("色笔记本", ControlIntent.NONE, at_ms=1100).allow)
