"""Suspended Windows process creation with a private kill-on-close Job Object."""
import os
import subprocess
import time


def _create_kill_on_close_job():
    import win32job
    job = win32job.CreateJobObject(None, '')
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job
    except BaseException:
        job.Close()
        raise


def bind_process_to_job(pid):
    """Best-effort binding for an existing child; caller must retain/close the job.

    Hub-owned executors still use suspended creation below so children cannot
    escape before assignment. Failure here leaves the original process running.
    """
    job = None
    try:
        import win32api, win32con, win32job
        job = _create_kill_on_close_job()
        process = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, process)
        finally:
            process.Close()
        return job
    except Exception:
        if job is not None:
            job.Close()
        return None


class WindowsProcess:
    def __init__(self, argv, cwd, env):
        import msvcrt
        import win32api, win32con, win32job, win32process
        self.job = _create_kill_on_close_job()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        null = open(os.devnull, 'rb')
        for fd in (out_w, err_w, null.fileno()): os.set_inheritable(fd, True)
        startup = win32process.STARTUPINFO()
        startup.dwFlags = win32con.STARTF_USESTDHANDLES | win32con.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        startup.hStdInput = msvcrt.get_osfhandle(null.fileno())
        startup.hStdOutput = msvcrt.get_osfhandle(out_w)
        startup.hStdError = msvcrt.get_osfhandle(err_w)
        try:
            handle, thread, self.pid, _ = win32process.CreateProcess(
                str(argv[0]), subprocess.list2cmdline(argv), None, None, True,
                win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW | win32con.CREATE_NEW_PROCESS_GROUP,
                env, str(cwd), startup)
            self.handle = handle
            try:
                win32job.AssignProcessToJobObject(self.job, handle)
                win32process.ResumeThread(thread)
            except BaseException:
                win32api.TerminateProcess(handle, 1)
                handle.Close()
                raise
            finally: thread.Close()
        except BaseException:
            self.job.Close()
            os.close(out_r); os.close(err_r)
            raise
        finally:
            os.close(out_w); os.close(err_w); null.close()
        self.stdout = os.fdopen(out_r, 'rb')
        self.stderr = os.fdopen(err_r, 'rb')

    def poll(self):
        import win32event, win32process
        if win32event.WaitForSingleObject(self.handle, 0) == win32event.WAIT_TIMEOUT: return None
        return win32process.GetExitCodeProcess(self.handle)

    def wait(self, timeout=None):
        until = time.monotonic() + timeout if timeout is not None else float('inf')
        while self.poll() is None:
            if time.monotonic() >= until: raise subprocess.TimeoutExpired('executor', timeout)
            time.sleep(.02)
        return self.poll()

    def stop_tree(self):
        import win32job
        win32job.TerminateJobObject(self.job, 1)
        self.wait(15)

    def close_tree(self):
        # Closing the last job handle also kills descendants after the root exited.
        self.job.Close()

    def close(self):
        self.stdout.close(); self.stderr.close(); self.handle.Close()
