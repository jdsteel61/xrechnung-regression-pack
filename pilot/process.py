"""Process-tree lifetime bound, NOT a network/filesystem sandbox.

`contained_run` dispatches by OS. On Windows (10+, pywin32) it uses a
kill-on-close job object; atomic JOB_LIST assignment prevents a parent crash
between creating a child and assigning it to the job
(https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812). On Linux it
uses a new session/process group plus PR_SET_PDEATHSIG, with post-kill /proc
confirmation that no group member survives. Both discard stdio, watch scratch
size, and enforce a hard timeout. Neither is a network or filesystem sandbox;
on Railway the container boundary is the outer isolation.
"""
import ctypes
import os
import json
from pathlib import Path
import subprocess
import time


def contained_run(command, *, cwd, timeout, scratch, max_scratch_bytes=512*1024*1024,
                  env=None, input=b'', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
    if input != b'' or stdout != subprocess.DEVNULL or stderr != subprocess.DEVNULL:
        raise ValueError('worker_stdio_must_be_discarded')
    runner = _contained_run_windows if os.name == 'nt' else _contained_run_posix
    return runner(command, cwd=cwd, timeout=timeout, scratch=scratch,
                  max_scratch_bytes=max_scratch_bytes, env=env)


def _contained_run_windows(command, *, cwd, timeout, scratch, max_scratch_bytes, env):
    # Windows-only handle/console types; imported here so the module stays
    # importable on Linux (where dispatch never reaches this path).
    from ctypes import wintypes as w
    import win32job

    class Startup(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('reserved', w.LPWSTR), ('desktop', w.LPWSTR), ('title', w.LPWSTR),
                    ('x', w.DWORD), ('y', w.DWORD), ('xs', w.DWORD), ('ys', w.DWORD),
                    ('xc', w.DWORD), ('yc', w.DWORD), ('fill', w.DWORD), ('flags', w.DWORD),
                    ('show', w.WORD), ('reserved2', w.WORD), ('bytes', ctypes.c_void_p),
                    ('stdin', w.HANDLE), ('stdout', w.HANDLE), ('stderr', w.HANDLE)]

    class StartupEx(ctypes.Structure):
        _fields_ = [('startup', Startup), ('attributes', ctypes.c_void_p)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [('process', w.HANDLE), ('thread', w.HANDLE), ('pid', w.DWORD), ('tid', w.DWORD)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'InitializeProcThreadAttributeList': ([ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
        'UpdateProcThreadAttribute': ([ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
        'DeleteProcThreadAttributeList': ([ctypes.c_void_p], None),
        'CreateProcessW': ([w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(StartupEx), ctypes.POINTER(ProcessInfo)], w.BOOL),
        'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
        'GetExitCodeProcess': ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        'CloseHandle': ([w.HANDLE], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        getattr(kernel, name).argtypes = args
        getattr(kernel, name).restype = result
    job = win32job.CreateJobObject(None, '')
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info['BasicLimitInformation']['LimitFlags'] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
    info['JobMemoryLimit'] = 2*1024*1024*1024
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    size = ctypes.c_size_t()
    kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
    attributes = ctypes.create_string_buffer(size.value)
    process = ProcessInfo()
    initialized = False
    try:
        if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        initialized = True
        jobs = (w.HANDLE*1)(int(job))
        if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x0002000D, jobs, ctypes.sizeof(jobs), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        startup = StartupEx()
        startup.startup.cb = ctypes.sizeof(startup)
        startup.startup.flags = 0x100  # STARTF_USESTDHANDLES, all NULL: discard diagnostics.
        startup.attributes = ctypes.addressof(attributes)
        environment = env if env is not None else {'SystemRoot': os.environ.get('SystemRoot', r'C:\Windows')}
        block = ctypes.create_unicode_buffer('\0'.join(k+'='+v for k, v in sorted(environment.items()))+'\0\0')
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline([str(c) for c in command]))
        if not kernel.CreateProcessW(str(command[0]), line, None, None, False,
                0x08000000 | 0x00080000 | 0x00000400, block, str(cwd), ctypes.byref(startup), ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        (Path(scratch)/'process.json').write_text(json.dumps({
            'pid': process.pid, 'executable': Path(command[0]).name}), encoding='utf-8')
        deadline = time.monotonic()+timeout
        while True:
            total = 0
            for path in Path(scratch).rglob('*'):
                if path.is_symlink() or path.is_junction():
                    raise OSError('worker_created_link')
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        continue  # Worker removed a temporary file during the scan.
                    if total > max_scratch_bytes:
                        raise OSError('scratch_limit')
            if kernel.WaitForSingleObject(process.process, 25) == 0:
                code = w.DWORD()
                if not kernel.GetExitCodeProcess(process.process, ctypes.byref(code)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return subprocess.CompletedProcess(command, code.value)
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('validator', timeout)
    finally:
        # Wait for every descendant to exit BEFORE caller removes scratch.
        win32job.TerminateJobObject(job, 1)
        if process.process and kernel.WaitForSingleObject(process.process, 10000) != 0:
            raise OSError('process_termination_unconfirmed')
        deadline = time.monotonic()+10
        while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
            if time.monotonic() >= deadline:
                raise OSError('process_tree_termination_unconfirmed')
            time.sleep(.01)
        if process.thread:
            kernel.CloseHandle(process.thread)
        if process.process:
            kernel.CloseHandle(process.process)
        if initialized:
            kernel.DeleteProcThreadAttributeList(attributes)
        job.Close()


def _pgid_alive(pgid):
    """True while any *executable* process still reports this process-group id.

    Used to confirm the job's whole process group is gone before the caller
    removes scratch. A zombie (state Z) or dead (X/x) member is already
    terminated — it holds no memory, file descriptors or scratch and can never
    run again — so it does NOT count as alive; on Railway the app is PID 1 and
    does not reap orphans, so a SIGKILLed descendant lingers as a harmless
    zombie until reaped (see the drain in `_contained_run_posix`). Best-effort:
    unlike the Windows kill-on-close job object, this is process-group +
    PR_SET_PDEATHSIG enforcement verified after the fact; the container boundary
    is the final backstop.
    """
    target = str(pgid).encode()
    try:
        names = os.listdir('/proc')
    except OSError:
        return False
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open('/proc/' + name + '/stat', 'rb') as handle:
                content = handle.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        end = content.rfind(b')')  # comm may contain spaces/parens
        if end < 0:
            continue
        fields = content[end + 2:].split()
        # After comm: state(0) ppid(1) pgrp(2) ...
        if len(fields) >= 3 and fields[2] == target and fields[0] not in (b'Z', b'X', b'x'):
            return True
    return False


def _contained_run_posix(command, *, cwd, timeout, scratch, max_scratch_bytes, env):
    """Linux job lifetime bound around a single validator process.

    Mirrors the Windows job object's guarantees with POSIX primitives: a new
    session/process group so the whole tree can be signalled at once,
    PR_SET_PDEATHSIG=SIGKILL so a parent (worker) crash tears the job down, no
    core dumps (official diagnostics may quote invoice values), a scratch-size
    watchdog, a hard timeout, and post-kill confirmation that no group member
    survives before scratch is released. All stdio is discarded.
    """
    import ctypes
    import ctypes.util
    import resource
    import signal

    libc = ctypes.CDLL(ctypes.util.find_library('c') or 'libc.so.6', use_errno=True)
    PR_SET_PDEATHSIG = 1
    PR_SET_CHILD_SUBREAPER = 36

    def preexec():
        os.setsid()  # new session; process-group id becomes this child's pid
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (256, 256))
        except (ValueError, OSError):
            pass

    # Become the reaper for orphaned group members. On Railway the app is PID 1
    # and does not reap; without this, a SIGKILLed grandchild (whose direct
    # parent we already reaped) would reparent to PID 1 and pile up as a zombie.
    # As subreaper it reparents to us instead, so the finally block can reap it.
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    environment = env if env is not None else {'PATH': os.environ.get('PATH', '')}
    proc = subprocess.Popen([str(c) for c in command], cwd=str(cwd), env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        preexec_fn=preexec, close_fds=True)
    (Path(scratch)/'process.json').write_text(json.dumps({
        'pid': proc.pid, 'executable': Path(command[0]).name}), encoding='utf-8')
    pgid = proc.pid  # setsid() set the group id equal to the child pid
    deadline = time.monotonic()+timeout
    try:
        while True:
            total = 0
            for path in Path(scratch).rglob('*'):
                if path.is_symlink():
                    raise OSError('worker_created_link')
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except FileNotFoundError:
                        continue  # Worker removed a temporary file during the scan.
                    if total > max_scratch_bytes:
                        raise OSError('scratch_limit')
            try:
                code = proc.wait(timeout=0.025)
                return subprocess.CompletedProcess(command, code)
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired('validator', timeout)
    finally:
        # Kill the whole group and confirm every descendant exits BEFORE the
        # caller removes scratch.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            raise OSError('process_termination_unconfirmed')
        # Reap group members that reparented to us (PR_SET_CHILD_SUBREAPER) so
        # they do not linger as zombies, and confirm no *live* member remains
        # before the caller removes scratch.
        end = time.monotonic()+10
        while True:
            try:
                while os.waitpid(-pgid, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass
            if not _pgid_alive(pgid):
                break
            if time.monotonic() >= end:
                raise OSError('process_tree_termination_unconfirmed')
            time.sleep(.01)
