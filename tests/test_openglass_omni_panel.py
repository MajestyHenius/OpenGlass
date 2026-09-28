from __future__ import annotations

import ast
import copy
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


# Import only the current manager/config: importing panel itself creates local
# certificates and reads machine-specific secrets. Neither belongs in a test.
SOURCE = Path(__file__).resolve().parents[1] / "runtime/openglass_omni/panel.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NODES = [node for node in TREE.body if
    isinstance(node, ast.ClassDef) and node.name == "ProcManager" or
    isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CONFIG" for t in node.targets)]
NAMESPACE = dict(globals(), __file__=str(SOURCE))
exec(compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec"), NAMESPACE)
ProcManager = NAMESPACE["ProcManager"]


class PanelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        cfg = copy.deepcopy(NAMESPACE["CONFIG"])
        cfg["proc_log_dir"] = self.temp.name
        cfg["llama_ready_timeout_s"] = 0.005
        with patch.object(threading.Thread, "start"):
            self.manager = ProcManager(cfg)
        self.manager.current_chain = "esp32_full"
        self.manager._sleep = Mock(return_value=True)

    def tearDown(self):
        self.temp.cleanup()

    def alive(self, name):
        self.manager.procs[name] = SimpleNamespace(pid=123, poll=lambda: None)

    def test_worker_log_does_not_override_failed_health(self):
        m = self.manager
        m._ready_events["worker"].set()
        m._http_get_json = Mock(return_value={"status": "error"})
        self.assertFalse(m._probe_ready("worker")[0])

    def test_exited_parent_still_cleans_owned_job(self):
        m = self.manager
        m.procs["worker"] = SimpleNamespace(poll=lambda: 0)
        job = Mock()
        m._jobs["worker"] = job
        self.assertTrue(m._kill("worker"))
        job.finish.assert_called_once()
        self.assertNotIn("worker", m._jobs)

    def test_failed_job_cleanup_does_not_report_stopped(self):
        m = self.manager
        m.procs["worker"] = SimpleNamespace(poll=lambda: 0)
        job = Mock()
        job.finish.side_effect = TimeoutError("child remains")
        m._jobs["worker"] = job
        self.assertFalse(m._kill("worker"))
        self.assertEqual(m.status["worker"], "crashed")
        self.assertIn("worker", m._jobs)

    def test_start_rejected_after_panel_shutdown(self):
        self.manager._shutting_down = True
        self.assertFalse(self.manager.start_all())
        self.assertFalse(self.manager._op_lock.locked())

    def test_panel_passes_actual_backend_url_to_runtime(self):
        m = self.manager
        m.cfg["llama_health_url"] = "http://127.0.0.1:22509/health"
        m._device_args = Mock(return_value=[])
        m._funnel_extra_args = Mock(return_value=[])
        cmd = m._build_cmd("demo_funnel")
        self.assertEqual(cmd[cmd.index("--backend-close-url") + 1], "http://127.0.0.1:22509")

    def test_stop_all_reports_incomplete_until_job_is_empty(self):
        m = self.manager
        job = Mock()
        job.finish.side_effect = TimeoutError("child remains")
        m._jobs["worker"] = job
        m._port_open = Mock(return_value=False)
        with patch.object(time, "sleep"):
            self.assertFalse(m._stop_all_sync())
        self.assertTrue(m._backend_recovery_required)
        self.assertEqual(m.status["worker"], "crashed")
        job.finish.side_effect = None
        with patch.object(time, "sleep"):
            self.assertTrue(m._stop_all_sync())
        self.assertFalse(m._backend_recovery_required)

    def test_gateway_defaults_to_probed_ipv4_worker(self):
        m = self.manager
        m.cfg["worker_health_port"] = 22409
        cmd = m._build_cmd("gateway")
        self.assertEqual(cmd[cmd.index("--workers") + 1], "127.0.0.1:22409")

    def test_panel_modes_keep_voice_and_silent_filtering_distinct(self):
        m = self.manager
        m.current_chain = "esp32_voice"
        self.assertEqual(m._funnel_extra_args(), [])
        m.current_chain = "esp32_select"
        self.assertIn("--no-reject", m._funnel_extra_args())
        m.current_chain = "esp32_full"
        args = m._funnel_extra_args()
        self.assertIn("--funnel", args)
        self.assertNotIn("--no-reject", args)
        self.assertNotIn("--force-measure", args)
        self.assertNotIn("--reject-wav-dir", args)
        m._reject_wav_dir = Mock(side_effect=AssertionError("silent mode must not require hint audio"))
        self.assertTrue(m._check_reject_wav())

    def test_gateway_preserves_explicit_worker_options(self):
        m = self.manager
        for args in (["--workers", "host:1234"], ["--workers=host:1234"],
                     ["--num-workers", "2"], ["--num-workers=2"]):
            m.cfg["procs"]["gateway"] = ["python", "gateway.py", *args]
            self.assertEqual(m._build_cmd("gateway")[2:], args)

    def test_child_proxy_bypass_preserves_existing_settings(self):
        with patch.object(os, "environ", {"HTTP_PROXY": "http://proxy:1234",
                                    "NO_PROXY": "internal.example",
                                    "no_proxy": "other.example"}):
            env = self.manager._child_env()
            self.assertEqual(env["HTTP_PROXY"], "http://proxy:1234")
            self.assertEqual(env["NO_PROXY"], env["no_proxy"])
            for host in ("localhost", "127.0.0.1", "::1", "internal.example", "other.example"):
                self.assertIn(host, env["NO_PROXY"].split(","))
            self.assertEqual(os.environ["NO_PROXY"], "internal.example")

    def test_gateway_with_only_offline_workers_is_not_ready(self):
        m = self.manager
        m._gateway_status = Mock(return_value={"gateway_healthy": True, "offline_workers": 1})
        self.assertFalse(m._probe_ready("gateway")[0])
        m._gateway_status.return_value = {"gateway_healthy": True, "idle_workers": 1}
        self.assertTrue(m._probe_ready("gateway")[0])

    def test_harness_requires_loaded_asr(self):
        m = self.manager
        m._http_get_json = Mock(return_value={"ok": True, "enabled": True, "asr_loaded": False})
        self.assertFalse(m._probe_ready("harness")[0])

    def test_alive_backend_does_not_pass_readiness_timeout(self):
        m = self.manager
        self.alive("llama")
        m._http_ok = Mock(return_value=False)
        self.assertFalse(m._wait_ready_or_die("llama", 0.005))

    def test_tail_requires_actual_session_ready(self):
        m = self.manager
        self.alive("demo_funnel")
        self.assertFalse(m._probe_ready("demo_funnel")[0])
        m._ready_events["demo_funnel"].set()
        self.assertTrue(m._probe_ready("demo_funnel")[0])

    def test_unknown_port_owner_blocks_launch_without_killing(self):
        m = self.manager
        m._port_open = Mock(side_effect=lambda port: port == 22500)
        m._kill_by_port = Mock()
        self.assertFalse(m._sweep_stale())
        m._kill_by_port.assert_not_called()

    def test_own_funnel_ui_is_not_treated_as_stale(self):
        m = self.manager
        self.alive("demo_funnel")
        m._port_open = Mock(side_effect=lambda port: port == 8080)
        self.assertTrue(m._sweep_stale())

    def test_failed_upstream_recheck_prevents_client_start(self):
        m = self.manager
        for stage in ("llama", "worker", "gateway", "harness"):
            self.alive(stage)
        m._sweep_stale = Mock(return_value=True)
        m._check_extensions = m._check_deps = m._check_reject_wav = Mock(return_value=True)
        m._wait_ready_or_die = Mock(side_effect=lambda name, timeout: name != "worker")
        m._start_one = Mock(return_value=True)
        m._do_start_all()
        m._start_one.assert_not_called()
        self.assertEqual(m.status["worker"], "crashed")

    def test_missing_backend_stops_downstream_before_start(self):
        m = self.manager
        for stage in ("worker", "gateway", "harness", "demo_funnel"):
            self.alive(stage)
        m._sweep_stale = Mock(return_value=True)
        m._check_extensions = m._check_deps = m._check_reject_wav = Mock(return_value=True)
        calls = []
        def kill(name, **kwargs):
            calls.append(("stop", name))
            m.procs.pop(name, None)
            return True
        def start(name):
            calls.append(("start", name))
            self.alive(name)
            return True
        m._kill, m._start_one = kill, start
        m._do_start_all()
        self.assertLess(calls.index(("stop", "worker")), calls.index(("start", "llama")))
        self.assertLess(calls.index(("stop", "demo_funnel")), calls.index(("start", "llama")))

    def test_configuration_does_not_change_during_launch(self):
        m = self.manager
        m._op_lock.acquire()
        try:
            self.assertFalse(m.configure(chain="esp32_voice", prompt="changed"))
            self.assertEqual(m.current_chain, "esp32_full")
        finally:
            m._op_lock.release()

    def test_restart_uses_normal_startup_checks(self):
        m = self.manager
        m._kill = Mock(return_value=True)
        m._do_start_all = Mock()
        with patch.object(threading, "Thread", side_effect=lambda target, **kw: SimpleNamespace(start=target)):
            self.assertTrue(m.restart_demo("test"))
        m._do_start_all.assert_called_once()
        self.assertFalse(m._op_lock.locked())

    def test_lifecycle_diagnostics_are_persisted(self):
        m = self.manager
        m._log("llama", "exit=3221225477 hex=0xC0000005")
        self.assertIn("0xC0000005", Path(m._panel_log_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
