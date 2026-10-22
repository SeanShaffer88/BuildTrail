"""Bounded streaming commands with owned process-tree lifetime."""
import ctypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time


class ProcessTree:
    def __init__(self, argv, cwd):
        self.job = None
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        if os.name == 'nt':
            flags |= 4  # CREATE_SUSPENDED: attach to Job before any child can spawn.
        # The frozen runtime's DLL search path must not leak into external tools.
        kernel = None
        if os.name == 'nt' and getattr(sys, 'frozen', False):
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            if not kernel.SetDllDirectoryW(None):
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags,
                start_new_session=os.name != 'nt')
        finally:
            if kernel:
                kernel.SetDllDirectoryW(sys._MEIPASS)
        try:
            if os.name == 'nt':
                self._attach_windows_job()
        except BaseException:
            self.process.kill()
            self.process.wait()
            self.close()
            self.process.stdout.close()
            raise

    def _attach_windows_job(self):
        from ctypes import wintypes as w
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel = k
        for name in ('CreateJobObjectW', 'OpenProcess', 'OpenThread', 'CreateToolhelp32Snapshot'):
            getattr(k, name).restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.ResumeThread.argtypes = [w.HANDLE]
        k.ResumeThread.restype = w.DWORD
        class Basic(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_longlong), ('per_job', ctypes.c_longlong),
                ('flags', w.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Io), ('process_limit', ctypes.c_size_t),
                ('job_limit', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        class ThreadEntry(ctypes.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('tid', w.DWORD), ('pid', w.DWORD),
                       ('priority', w.LONG), ('base', w.LONG), ('flags', w.DWORD)]
        k.Thread32First.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        k.Thread32Next.argtypes = [w.HANDLE, ctypes.POINTER(ThreadEntry)]
        self.job = k.CreateJobObjectW(None, None)
        if not self.job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        handle = k.OpenProcess(0x0100 | 0x0001, False, self.process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not k.AssignProcessToJobObject(self.job, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            k.CloseHandle(handle)
        snapshot = k.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = k.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.pid == self.process.pid:
                    thread = k.OpenThread(2, False, entry.tid)
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if k.ResumeThread(thread) == 0xffffffff:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        k.CloseHandle(thread)
                    return
                more = k.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError('Could not resume the owned build process.')
        finally:
            k.CloseHandle(snapshot)

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def execute(argv, cwd, log_path, timeout, cancelled=lambda: False, output=lambda text: None,
            max_log_bytes=10 * 1024 * 1024):
    start = time.monotonic()
    tree = ProcessTree(argv, cwd)
    chunks = queue.Queue(maxsize=64)
    stopped = threading.Event()
    reader_error = []
    def put(chunk):
        while not stopped.is_set():
            try:
                chunks.put(chunk, timeout=.05)
                return
            except queue.Full:
                pass
    def read():
        try:
            while chunk := tree.process.stdout.read1(4096):
                put(chunk)
                if stopped.is_set():
                    break
        except OSError as exc:
            reader_error.append(str(exc))
        finally:
            put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    state, written, total = 'completed', 0, 0
    try:
        with open(log_path, 'wb') as stream:
            while True:
                if cancelled() and state == 'completed':
                    state = 'cancelled'
                    tree.close()
                elif time.monotonic() - start >= timeout and state == 'completed' and tree.process.poll() is None:
                    state = 'timed_out'
                    tree.close()
                if tree.process.poll() is not None:
                    tree.close()  # Do not leave background descendants holding the log pipe.
                try:
                    chunk = chunks.get(timeout=.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                total += len(chunk)
                kept = chunk[:max(0, max_log_bytes - written)]
                stream.write(kept)
                written += len(kept)
                if kept:
                    output(kept.decode('utf-8', errors='replace'))
        # A process can close stdout and keep running; cancellation/timeout still apply.
        while tree.process.poll() is None:
            if cancelled():
                state = 'cancelled'
                tree.close()
            elif time.monotonic() - start >= timeout:
                state = 'timed_out'
                tree.close()
            time.sleep(.02)
        if reader_error:
            raise OSError('Log capture failed: ' + reader_error[0])
        return {'exit_code': tree.process.returncode, 'state': state,
                'duration_seconds': round(time.monotonic() - start, 3),
                'log_bytes': written, 'log_truncated': total > written}
    finally:
        tree.close()
        stopped.set()
        tree.process.wait(timeout=10)
        reader.join(timeout=5)
        tree.process.stdout.close()
