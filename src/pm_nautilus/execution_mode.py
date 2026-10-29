"""Durable execution-mode intent only; never unlocks or starts an account."""

from contextlib import suppress
import json
import os
from pathlib import Path
import stat
from uuid import uuid4


class ExecutionModeStore:
    VERSION = 1
    MAX_BYTES = 256
    FILENAME = "execution-mode.json"

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / self.FILENAME

    def _directory(self, *, create=False):
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        try:
            return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            if not create:
                return None
            raise
        except OSError as exc:
            raise ValueError("运行模式配置目录必须为真实目录") from exc

    def _raw(self, directory):
        try:
            descriptor = os.open(
                self.FILENAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError("运行模式配置必须为普通文件，禁止符号链接") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.MAX_BYTES:
                raise ValueError("运行模式配置文件类型或大小无效")
            raw = os.read(descriptor, self.MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(raw) > self.MAX_BYTES:
            raise ValueError("运行模式配置过大")
        return raw

    @staticmethod
    def _mode(mode):
        if type(mode) is not str or mode not in ("TEST", "LIVE"):
            raise ValueError("运行模式仅接受TEST或LIVE")
        return mode

    def _decode(self, raw):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("运行模式配置包含重复字段")
                result[key] = value
            return result

        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("运行模式配置不是有效JSON") from exc
        if (
            type(value) is not dict
            or set(value) != {"version", "mode"}
            or type(value["version"]) is not int
            or value["version"] != self.VERSION
        ):
            raise ValueError("运行模式配置版本或字段无效")
        return self._mode(value["mode"])

    def read(self):
        directory = self._directory()
        if directory is None:
            return "TEST"
        try:
            raw = self._raw(directory)
            return "TEST" if raw is None else self._decode(raw)
        finally:
            os.close(directory)

    def _stage(self, directory, raw):
        name = f".execution-mode-{uuid4().hex}.tmp"
        complete = False
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            complete = True
            return name
        finally:
            if not complete:
                with suppress(OSError):
                    os.unlink(name, dir_fd=directory)

    def save(self, mode):
        """Atomically replace intent; post-replace fsync errors remain uncertain.

        Before replace, failures preserve the old file. After replace, any error
        must keep execution stopped; it must not trigger a second mode change.
        """
        mode = self._mode(mode)
        directory = self._directory(create=True)
        staged = None
        try:
            previous = self._raw(directory)
            if previous is not None:
                self._decode(previous)
            raw = (
                json.dumps({"version": self.VERSION, "mode": mode}, separators=(",", ":")) + "\n"
            ).encode()
            staged = self._stage(directory, raw)
            if self._raw(directory) != previous:
                raise RuntimeError("运行模式配置在保存期间发生变化")
            os.replace(staged, self.FILENAME, src_dir_fd=directory, dst_dir_fd=directory)
            staged = None
            os.fsync(directory)
        finally:
            if staged is not None:
                with suppress(OSError):
                    os.unlink(staged, dir_fd=directory)
            os.close(directory)
