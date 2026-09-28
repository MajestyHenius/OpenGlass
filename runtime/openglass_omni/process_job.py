"""Own a Windows child-process tree, including children outliving their parent."""
import ctypes
from ctypes import wintypes as w
import time


class WindowsProcessJob:
    def __init__(self):
        k = self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD], w.BOOL),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL),
            ("QueryInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result

        class Limits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Limits), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            k.CloseHandle(self.handle)
            self.handle = None
            raise error

    def assign(self, proc):
        if not self.k.AssignProcessToJobObject(self.handle, w.HANDLE(int(proc._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_count(self):
        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]
        info = Accounting()
        if not self.k.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def finish(self, timeout=5.0):
        """Kill any remaining descendants and confirm exit before releasing ownership."""
        if not self.handle:
            return
        if not self.k.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while self.active_count():
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows process job still has live children")
            time.sleep(0.05)
        self.k.CloseHandle(self.handle)
        self.handle = None
