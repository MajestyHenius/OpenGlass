"""Exercise Windows ownership with disposable Python processes, no model required."""
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import sys
import unittest

from runtime.openglass_omni.process_job import WindowsProcessJob


@unittest.skipUnless(os.name == "nt", "Windows Job Objects")
class WindowsJobTests(unittest.TestCase):
    def open_process(self, pid):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes, k.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        k.WaitForSingleObject.argtypes, k.WaitForSingleObject.restype = [w.HANDLE, w.DWORD], w.DWORD
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.OpenProcess(0x100000, False, pid)
        self.assertTrue(handle, ctypes.get_last_error())
        self.addCleanup(k.CloseHandle, handle)
        return k, handle

    def cleanup_process(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def test_orphan_child_is_terminated_after_parent_exits(self):
        script = (
            "import subprocess,sys; sys.stdin.readline(); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(p.pid,flush=True); sys.stdin.readline()"
        )
        proc = subprocess.Popen([sys.executable, "-u", "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process, proc)
        job = WindowsProcessJob()
        self.addCleanup(job.finish)
        job.assign(proc)
        proc.stdin.write("spawn\n"); proc.stdin.flush()
        child_pid = int(proc.stdout.readline())
        k, child = self.open_process(child_pid)
        proc.stdin.write("exit\n"); proc.stdin.flush()
        proc.wait(timeout=5)
        self.assertEqual(job.active_count(), 1)
        job.finish()
        self.assertEqual(k.WaitForSingleObject(child, 3000), 0)

    def test_abrupt_owner_exit_kills_owned_process(self):
        script = (
            "import os,subprocess,sys; "
            "from runtime.openglass_omni.process_job import WindowsProcessJob; "
            "j=WindowsProcessJob(); "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "j.assign(p); print(p.pid,flush=True); sys.stdin.readline(); os._exit(0)"
        )
        proc = subprocess.Popen([sys.executable, "-B", "-u", "-c", script], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[1])
        self.addCleanup(self.cleanup_process, proc)
        k, child = self.open_process(int(proc.stdout.readline()))
        proc.stdin.write("crash\n"); proc.stdin.flush()
        proc.wait(timeout=5)
        self.assertEqual(k.WaitForSingleObject(child, 3000), 0)
