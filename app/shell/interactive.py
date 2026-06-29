from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import re
import signal
import struct
import termios
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


# Strip ANSI escape sequences and control chars before posting to Matrix.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CTL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_for_matrix(s: str) -> str:
    s = _ANSI_RE.sub("", s)
    s = _CTL_RE.sub("", s)
    # CR-LF / lone CR normalization
    return s.replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class InteractiveShell:
    room_id: str
    cwd: str
    command: str
    on_output: Callable[[str], Awaitable[None]]
    on_exit: Callable[[int], Awaitable[None]]
    master_fd: int | None = None
    pid: int | None = None
    exit_code: int | None = None
    _flush_task: asyncio.Task[None] | None = None
    _output_buf: bytearray = field(default_factory=bytearray)
    _stopped: bool = False

    async def start(self) -> None:
        master, slave = pty.openpty()
        # Reasonable initial size; Matrix has no concept of cols anyway.
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        except OSError:
            pass

        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                os.dup2(slave, 0)
                os.dup2(slave, 1)
                os.dup2(slave, 2)
                if slave > 2:
                    os.close(slave)
                if master > 2:
                    os.close(master)
                env = os.environ.copy()
                env["TERM"] = "dumb"
                env["PS1"] = "$ "
                os.chdir(self.cwd)
                os.execvpe("/bin/bash", ["bash", "-lc", self.command], env)
            except Exception:
                os._exit(127)

        os.close(slave)
        fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)
        self.master_fd = master
        self.pid = pid

        loop = asyncio.get_running_loop()
        loop.add_reader(master, self._on_readable)
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("shell start room=%s pid=%s cmd=%r", self.room_id, pid, self.command[:120])

    def _on_readable(self) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(self.master_fd, 8192)
        except BlockingIOError:
            return
        except OSError:
            self._teardown_reader()
            return
        if not data:
            self._teardown_reader()
            return
        self._output_buf.extend(data)

    def _teardown_reader(self) -> None:
        if self.master_fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.master_fd)
            except Exception:
                pass
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
        if not self._stopped:
            self._stopped = True

    async def _flush_loop(self) -> None:
        """Batch stdout for ~250ms or 1500 bytes, then post."""
        try:
            while True:
                await asyncio.sleep(0.25)
                # check process status
                if self.pid and self.exit_code is None:
                    try:
                        wpid, status = os.waitpid(self.pid, os.WNOHANG)
                        if wpid != 0:
                            self.exit_code = (
                                os.WEXITSTATUS(status)
                                if os.WIFEXITED(status)
                                else 128 + os.WTERMSIG(status) if os.WIFSIGNALED(status) else -1
                            )
                    except ChildProcessError:
                        self.exit_code = -1

                # flush whatever output we have
                await self._flush_now()

                if self._stopped and not self._output_buf and self.exit_code is not None:
                    break
                if self.exit_code is not None and not self._output_buf:
                    # process gone, no more output buffered, but reader might still
                    # have a final read pending — give it one more tick.
                    await asyncio.sleep(0.1)
                    await self._flush_now()
                    if not self._output_buf:
                        break
        except asyncio.CancelledError:
            await self._flush_now()
            raise
        finally:
            self._teardown_reader()
            try:
                await self.on_exit(self.exit_code if self.exit_code is not None else -1)
            except Exception:
                logger.exception("shell on_exit callback failed")

    async def _flush_now(self) -> None:
        if not self._output_buf:
            return
        chunk = bytes(self._output_buf)
        self._output_buf.clear()
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            text = chunk.decode("latin-1", errors="replace")
        cleaned = _clean_for_matrix(text)
        if not cleaned.strip():
            return
        # Matrix message size limit is generous (~64k) but be polite.
        for i in range(0, len(cleaned), 3500):
            await self.on_output(cleaned[i : i + 3500])

    def write(self, data: str) -> bool:
        """Send a line of input to the shell. Returns False if shell is gone."""
        if self.master_fd is None:
            return False
        try:
            os.write(self.master_fd, data.encode("utf-8"))
            return True
        except OSError:
            return False

    def send_signal(self, sig: signal.Signals) -> bool:
        if not self.pid:
            return False
        try:
            os.killpg(os.getpgid(self.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    async def kill(self) -> None:
        self.send_signal(signal.SIGTERM)
        await asyncio.sleep(0.3)
        if self.exit_code is None:
            self.send_signal(signal.SIGKILL)
        if self._flush_task and not self._flush_task.done():
            try:
                await asyncio.wait_for(self._flush_task, timeout=1.5)
            except asyncio.TimeoutError:
                self._flush_task.cancel()


# room_id → active shell
_active: dict[str, InteractiveShell] = {}


def get_active(room_id: str) -> InteractiveShell | None:
    return _active.get(room_id)


def set_active(room_id: str, sh: InteractiveShell | None) -> None:
    if sh is None:
        _active.pop(room_id, None)
    else:
        _active[room_id] = sh
