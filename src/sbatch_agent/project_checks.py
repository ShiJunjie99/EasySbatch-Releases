"""Recheck Scanner fingerprints and explicit project paths without executing files."""

import hashlib
import os
from pathlib import Path
import stat

from .scanner import ProjectScanError, _open_root, _DIR_FLAGS, _FILE_FLAGS


class ProjectChangedError(ValueError):
    pass


def project_path(root, value):
    path = Path(value)
    if ".." in path.parts:
        raise ProjectChangedError("项目路径不能包含 '..'。请重新分析或修正路径。")
    path = path if path.is_absolute() else Path(root) / path
    try:
        path.relative_to(root)
    except ValueError:
        raise ProjectChangedError("Smart Mode 的工作目录和项目文件必须位于项目根目录内。") from None
    return path


def _open(root_fd, relative, directory=False):
    fd = os.dup(root_fd)
    try:
        parts = Path(relative).parts
        for i, part in enumerate(parts):
            child = os.open(part, _DIR_FLAGS if directory or i < len(parts) - 1 else _FILE_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def check_project(evidence, config, *, directories=(), files=()):
    """Bounded hashes of originally-used text; metadata only for explicit inputs.

    Uses the Scanner's held-descriptor/no-symlink root policy. This detects
    changes, not a filesystem lock against edits after this check completes.
    """
    try:
        root, root_fd = _open_root(evidence.project_dir)
        try:
            total = 0
            for item in evidence.source_fingerprints:
                relative = project_path(root, item.path).relative_to(root)
                fd = _open(root_fd, relative)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > config.max_file_size:
                        raise ProjectChangedError("Project files changed since analysis. Please re-analyze.")
                    content = stream.read(config.max_file_size + 1)
                    after = os.fstat(stream.fileno())
                total += len(content)
                if (total > config.max_total_text_bytes or len(content) > config.max_file_size
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or hashlib.sha256(content).hexdigest() != item.sha256):
                    raise ProjectChangedError("Project files changed since analysis. Please re-analyze.")
            for directory, values in ((True, directories), (False, files)):
                for value in values:
                    fd = _open(root_fd, project_path(root, value).relative_to(root), directory)
                    try:
                        if not directory and not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ProjectChangedError("项目输入必须是可读普通文件。")
                    finally:
                        os.close(fd)
        finally:
            os.close(root_fd)
    except (OSError, ProjectScanError) as exc:
        raise ProjectChangedError("Project files changed or paths are unavailable. Please re-analyze；不跟随符号链接。") from exc
