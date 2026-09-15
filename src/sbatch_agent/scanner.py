"""Deterministic project evidence from bounded, read-only filesystem access.

No project imports, execution, environment resolution, Slurm calls or JobSpecs.
All symlinks are skipped. POSIX opens are relative to held directory descriptors
with O_NOFOLLOW. Windows also rejects reparse points and verifies file identity
before reading; its standard library does not expose equivalent directory fds.
"""

import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from datetime import datetime, timezone

from .scanner_models import (
    EvidenceLevel, FileFingerprint, ProjectEvidence, ScanConfig, ScannedFile,
)
from .scanner_detectors import (
    Document, EvidenceBuilder, PRIORITY, SHELL_SUFFIXES, detect_document,
    file_priority, relevant_text,
)


class ProjectScanError(ValueError):
    """The root cannot be safely scanned; message is suitable for the local UI."""


_DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_BINARY", 0)
)
_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_like(info):
    """Recognize symlinks plus Windows junctions and other reparse points."""
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
    )


def _open_root(project_dir):
    try:
        value = os.fspath(project_dir)
    except TypeError as exc:
        raise ProjectScanError("Project directory 必须是有效的绝对目录路径。") from exc
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ProjectScanError("Project directory 不能为空或含控制字符。")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ProjectScanError("Project directory 必须是绝对路径，且不能包含 '..'。")
    if path == Path(path.anchor):
        raise ProjectScanError("不能将整个文件系统根目录作为项目扫描。")
    fd = os.open(path.anchor, _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
    except OSError as exc:
        os.close(fd)
        message = ("Project directory not found：项目目录不存在。" if isinstance(exc, FileNotFoundError)
                   else "Permission denied：当前进程无权读取项目目录。" if isinstance(exc, PermissionError)
                   else "Project directory 必须是普通目录，路径各级不能包含符号链接。")
        raise ProjectScanError(message) from exc
    return path, fd


class _Scan:
    """One invocation's state; ProjectScanner instances can be reused safely."""

    def __init__(self, config):
        self.config = config
        self.out = EvidenceBuilder(config)
        self.files = []
        self.directories = 0
        self.skipped_dirs = []
        self.bytes_read = 0
        self.documents = []
        self.raw_documents = {}
        self.git_present = False
        self.stopped = False

    def skip_file(self, path, size, reason, *, warn=False):
        self.files.append(ScannedFile(path=path, size_bytes=size, status="skipped", reason=reason))
        if warn:
            self.out.warnings.add(f"{path}: {reason}；已跳过。")

    def list_directory(self, fd, path):
        # If this directory is too wide, discard the WHOLE enumeration. A
        # filesystem-order-dependent first N subset would not be deterministic.
        names = []
        try:
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if len(names) >= self.config.max_directory_entries:
                        self.out.limit("max_directory_entries")
                        self.skipped_dirs.append(path or ".")
                        return None
                    names.append(entry.name)
        except OSError as exc:
            if not path:
                raise ProjectScanError("Permission denied 或无法列出项目根目录。") from exc
            self.out.warnings.add(f"{path or '.'}: 无权限或无法列出目录；已跳过。")
            self.skipped_dirs.append(path or ".")
            return None
        return sorted(names, key=file_priority)

    def walk(self, fd, path="", depth=0):
        if self.stopped:
            return
        if self.directories >= self.config.max_directories:
            self.out.limit("max_directories")
            self.skipped_dirs.append(path or ".")
            return
        self.directories += 1
        names = self.list_directory(fd, path)
        if names is None:
            return
        subdirs = []
        for name in names:
            if self.stopped:
                return
            relative = str(PurePosixPath(path) / name)
            try:
                name.encode("utf-8")
            except UnicodeError:
                self.out.warnings.add(f"{path or '.'}: 文件名不是有效 UTF-8，已跳过。")
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                if len(self.files) >= self.config.max_files:
                    self.out.limit("max_files")
                    self.stopped = True
                    return
                self.skip_file(relative, None, "metadata 无法读取", warn=True)
                continue
            if not path and name == ".git":
                self.git_present = True  # presence only, never inspect HEAD/history
                if not stat.S_ISDIR(info.st_mode):
                    if len(self.files) >= self.config.max_files:
                        self.out.limit("max_files")
                        self.stopped = True
                        return
                    self.skip_file(relative, info.st_size, "Git metadata，仅记录存在")
                    continue
            if stat.S_ISDIR(info.st_mode):
                if name == ".git" or name in self.config.ignore_dirs:
                    self.skipped_dirs.append(relative)
                elif depth >= self.config.max_depth:
                    self.out.limit("max_depth")
                    self.skipped_dirs.append(relative)
                else:
                    subdirs.append((name, relative, info))
                continue
            if len(self.files) >= self.config.max_files:
                self.out.limit("max_files")
                self.stopped = True
                return
            if stat.S_ISLNK(info.st_mode):
                self.skip_file(relative, info.st_size, "symlink（包括内部、外部和循环链接）", warn=True)
            elif not stat.S_ISREG(info.st_mode):
                self.skip_file(relative, info.st_size, "不是普通文件（不读取 FIFO/socket/device）", warn=True)
            else:
                self.read_file(fd, name, relative, info)
        for name, relative, original in sorted(subdirs):
            if self.stopped:
                break
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError:
                self.out.warnings.add(f"{relative}: 目录无法安全打开（权限不足或已变化）；已跳过。")
                self.skipped_dirs.append(relative)
                continue
            try:
                current = os.fstat(child)
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    self.out.warnings.add(f"{relative}: 扫描期间目录发生变化；已跳过。")
                    self.skipped_dirs.append(relative)
                    continue
                self.walk(child, relative, depth + 1)
            finally:
                os.close(child)

    def executable_hint(self, path, info, text=None):
        suffix = PurePosixPath(path).suffix.lower()
        if not info.st_mode & 0o111 or suffix in SHELL_SUFFIXES | {".py", ".so", ".o", ".a"} or ".so." in path:
            return
        if text is not None and text.startswith("#!"):
            return
        self.out.emit("executable_candidates", "executable_bit", path, Document(path, ""),
                      description="普通文件具有 executable bit；仅 metadata 候选，格式与可运行性未验证。",
                      level=EvidenceLevel.INFERRED, priority=PRIORITY["metadata"])

    def read_file(self, fd, name, path, original):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in self.config.binary_suffixes or ".so." in name:
            self.skip_file(path, original.st_size, "binary/archive/data extension")
            return
        if original.st_size > self.config.max_file_size:
            self.out.limit("max_file_size")
            self.skip_file(path, original.st_size, "超过单文件大小限制", warn=True)
            self.executable_hint(path, original)
            return
        if not relevant_text(path):
            self.files.append(ScannedFile(path=path, size_bytes=original.st_size,
                                          status="metadata_only", reason="非目标文本类型"))
            self.executable_hint(path, original)
            return
        remaining = self.config.max_total_text_bytes - self.bytes_read
        if original.st_size > remaining:
            self.out.limit("max_total_text_bytes")
            self.skip_file(path, original.st_size, "剩余总读取预算不足")
            # Continue inventory/metadata, but do not spend a partial read on
            # a document whose fingerprint/evidence would be incomplete.
            return
        try:
            handle = os.open(name, _FILE_FLAGS, dir_fd=fd)
        except OSError:
            self.skip_file(path, original.st_size, "无法安全打开（权限不足或已变化）", warn=True)
            return
        try:
            before = os.fstat(handle)
            if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns,
            ):
                self.skip_file(path, original.st_size, "扫描期间文件发生变化", warn=True)
                return
            chunks = []
            count = 0
            while count < before.st_size:
                chunk = os.read(handle, min(65536, before.st_size - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                self.bytes_read += len(chunk)
            after = os.fstat(handle)
            if count != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                before.st_size, before.st_mtime_ns, before.st_ctime_ns,
            ):
                self.skip_file(path, before.st_size, "读取期间文件发生变化，未分析或计算指纹", warn=True)
                return
            raw = b"".join(chunks)
        except OSError:
            self.skip_file(path, original.st_size, "读取失败", warn=True)
            return
        finally:
            os.close(handle)
        if b"\x00" in raw:
            self.skip_file(path, len(raw), "NUL byte / binary content", warn=True)
            self.executable_hint(path, original)
            return
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            self.skip_file(path, len(raw), "不是有效 UTF-8 文本", warn=True)
            return
        if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            self.skip_file(path, len(raw), "binary control bytes", warn=True)
            return
        doc = Document(path, text.replace("\r\n", "\n").replace("\r", "\n"))
        self.documents.append(doc)
        self.raw_documents[path] = raw
        self.files.append(ScannedFile(path=path, size_bytes=len(raw), status="read"))
        self.executable_hint(path, original, text)


class _PortableScan(_Scan):
    """Windows-compatible walker with the same limits and detector output.

    Windows Python does not implement the POSIX ``dir_fd`` operations used by
    ``_Scan``. Every path component and entry is therefore inspected with
    ``lstat``; reparse points are rejected, and regular files are identity-
    checked again after opening and after reading.
    """

    def list_directory_path(self, directory, path):
        names = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if len(names) >= self.config.max_directory_entries:
                        self.out.limit("max_directory_entries")
                        self.skipped_dirs.append(path or ".")
                        return None
                    names.append(entry.name)
        except OSError as exc:
            if not path:
                raise ProjectScanError("Permission denied 或无法列出项目根目录。") from exc
            self.out.warnings.add(f"{path or '.'}: 无权限或无法列出目录；已跳过。")
            self.skipped_dirs.append(path or ".")
            return None
        return sorted(names, key=file_priority)

    def walk_path(self, root, directory, path="", depth=0):
        if self.stopped:
            return
        if self.directories >= self.config.max_directories:
            self.out.limit("max_directories")
            self.skipped_dirs.append(path or ".")
            return
        self.directories += 1
        names = self.list_directory_path(directory, path)
        if names is None:
            return
        subdirs = []
        for name in names:
            if self.stopped:
                return
            relative = str(PurePosixPath(path) / name)
            try:
                name.encode("utf-8")
            except UnicodeError:
                self.out.warnings.add(f"{path or '.'}: 文件名不是有效 UTF-8，已跳过。")
                continue
            child = directory / name
            try:
                info = os.lstat(child)
            except OSError:
                if len(self.files) >= self.config.max_files:
                    self.out.limit("max_files")
                    self.stopped = True
                    return
                self.skip_file(relative, None, "metadata 无法读取", warn=True)
                continue
            if not path and name == ".git":
                self.git_present = True
            if _is_link_like(info):
                if len(self.files) >= self.config.max_files:
                    self.out.limit("max_files")
                    self.stopped = True
                    return
                self.skip_file(relative, info.st_size, "symlink/reparse point", warn=True)
                continue
            if stat.S_ISDIR(info.st_mode):
                if name == ".git" or name in self.config.ignore_dirs:
                    self.skipped_dirs.append(relative)
                elif depth >= self.config.max_depth:
                    self.out.limit("max_depth")
                    self.skipped_dirs.append(relative)
                else:
                    subdirs.append((name, relative, info))
                continue
            if len(self.files) >= self.config.max_files:
                self.out.limit("max_files")
                self.stopped = True
                return
            if not stat.S_ISREG(info.st_mode):
                self.skip_file(relative, info.st_size, "不是普通文件（不读取 FIFO/socket/device）", warn=True)
            else:
                self.read_file_path(child, relative, info)
        for name, relative, original in sorted(subdirs):
            if self.stopped:
                break
            child = directory / name
            try:
                current = os.lstat(child)
                if _is_link_like(current) or not stat.S_ISDIR(current.st_mode) or (
                    current.st_dev, current.st_ino
                ) != (original.st_dev, original.st_ino):
                    raise OSError("directory identity changed")
                resolved = child.resolve(strict=True)
                if resolved != root and root not in resolved.parents:
                    raise OSError("directory escaped project root")
            except OSError:
                self.out.warnings.add(f"{relative}: 目录无法安全打开（权限不足或已变化）；已跳过。")
                self.skipped_dirs.append(relative)
                continue
            self.walk_path(root, child, relative, depth + 1)

    def read_file_path(self, filename, path, original):
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in self.config.binary_suffixes or ".so." in filename.name:
            self.skip_file(path, original.st_size, "binary/archive/data extension")
            return
        if original.st_size > self.config.max_file_size:
            self.out.limit("max_file_size")
            self.skip_file(path, original.st_size, "超过单文件大小限制", warn=True)
            self.executable_hint(path, original)
            return
        if not relevant_text(path):
            self.files.append(ScannedFile(path=path, size_bytes=original.st_size,
                                          status="metadata_only", reason="非目标文本类型"))
            self.executable_hint(path, original)
            return
        remaining = self.config.max_total_text_bytes - self.bytes_read
        if original.st_size > remaining:
            self.out.limit("max_total_text_bytes")
            self.skip_file(path, original.st_size, "剩余总读取预算不足")
            return
        try:
            current = os.lstat(filename)
            if _is_link_like(current) or not stat.S_ISREG(current.st_mode) or (
                current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns
            ) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
                raise OSError("file identity changed")
            handle = os.open(filename, _FILE_FLAGS)
        except OSError:
            self.skip_file(path, original.st_size, "无法安全打开（权限不足或已变化）", warn=True)
            return
        try:
            before = os.fstat(handle)
            if not stat.S_ISREG(before.st_mode) or (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
            ) != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns):
                self.skip_file(path, original.st_size, "扫描期间文件发生变化", warn=True)
                return
            chunks = []
            count = 0
            while count < before.st_size:
                chunk = os.read(handle, min(65536, before.st_size - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                self.bytes_read += len(chunk)
            after = os.fstat(handle)
            if count != before.st_size or (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                self.skip_file(path, before.st_size, "读取期间文件发生变化，未分析或计算指纹", warn=True)
                return
            raw = b"".join(chunks)
        except OSError:
            self.skip_file(path, original.st_size, "读取失败", warn=True)
            return
        finally:
            os.close(handle)
        if b"\x00" in raw:
            self.skip_file(path, len(raw), "NUL byte / binary content", warn=True)
            self.executable_hint(path, original)
            return
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            self.skip_file(path, len(raw), "不是有效 UTF-8 文本", warn=True)
            return
        if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            self.skip_file(path, len(raw), "binary control bytes", warn=True)
            return
        doc = Document(path, text.replace("\r\n", "\n").replace("\r", "\n"))
        self.documents.append(doc)
        self.raw_documents[path] = raw
        self.files.append(ScannedFile(path=path, size_bytes=len(raw), status="read"))
        self.executable_hint(path, original, text)


def _open_portable_root(project_dir):
    try:
        value = os.fspath(project_dir)
    except TypeError as exc:
        raise ProjectScanError("Project directory 必须是有效的绝对目录路径。") from exc
    if not isinstance(value, str) or not value.strip() or not value.isprintable():
        raise ProjectScanError("Project directory 不能为空或含控制字符。")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ProjectScanError("Project directory 必须是绝对路径，且不能包含 '..'。")
    path = Path(os.path.abspath(path))
    if path == Path(path.anchor):
        raise ProjectScanError("不能将整个文件系统根目录作为项目扫描。")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            info = os.lstat(current)
            if _is_link_like(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError("root component is not a plain directory")
    except OSError as exc:
        message = ("Project directory not found：项目目录不存在。" if isinstance(exc, FileNotFoundError)
                   else "Permission denied：当前进程无权读取项目目录。" if isinstance(exc, PermissionError)
                   else "Project directory 必须是普通目录，路径各级不能包含符号链接或重解析点。")
        raise ProjectScanError(message) from exc
    return path


def _finalize_scan(root, scanned_at, scan):
    for doc in sorted(scan.documents, key=lambda d: file_priority(d.path)):
        detect_document(doc, scan.out)
    scan.out.finalize_inputs(scan.documents)
    groups = scan.out.candidates()
    if len(groups.get("entrypoint_candidates", ())) > 1:
        scan.out.ambiguities.add("存在多个入口候选；扫描器不选择唯一入口。")
    if not groups.get("input_candidates"):
        scan.out.ambiguities.add("未找到可安全读取并支持为输入候选的文件；不代表程序不需要输入。")
    if not scan.out.items:
        scan.out.warnings.add("No relevant project evidence found：未找到相关项目证据。")
    items = tuple(sorted(scan.out.items.values(), key=lambda e: (e.source_path, e.line_start or 0, e.kind, e.id)))
    used_paths = {item.source_path for item in items}
    return ProjectEvidence(
        project_dir=str(root), scanned_at=scanned_at, files_considered=len(scan.files),
        files_skipped=sum(f.status == "skipped" for f in scan.files), bytes_read=scan.bytes_read,
        files=tuple(sorted(scan.files, key=lambda f: f.path)),
        skipped_directories=tuple(sorted(set(scan.skipped_dirs))), git_present=scan.git_present,
        evidence_items=items, warnings=tuple(sorted(scan.out.warnings)),
        limits_reached=tuple(sorted(scan.out.limits)), ambiguities=tuple(sorted(scan.out.ambiguities)),
        source_fingerprints=tuple(FileFingerprint(path=p, sha256=hashlib.sha256(scan.raw_documents[p]).hexdigest())
                                  for p in sorted(used_paths & scan.raw_documents.keys())), **groups,
    )


class ProjectScanner:
    """Inspect a local absolute project path without following any symlinks.

    Reading a root is explicit authority to inspect that root, not all of home
    or the server. Callers must retain the current trusted single-user boundary.
    The scanner never opens paths extracted from project text.
    """

    def __init__(self, config: ScanConfig | None = None):
        self.config = config or ScanConfig()
        if not isinstance(self.config, ScanConfig):
            raise TypeError("config must be ScanConfig")

    def scan(self, project_dir: str | os.PathLike[str]) -> ProjectEvidence:
        if os.name == "nt":
            return self._scan_portable(project_dir)
        root, fd = _open_root(project_dir)
        scanned_at = datetime.now(timezone.utc)
        scan = _Scan(self.config)
        try:
            scan.walk(fd)
        finally:
            os.close(fd)
        return _finalize_scan(root, scanned_at, scan)

    def _scan_portable(self, project_dir: str | os.PathLike[str]) -> ProjectEvidence:
        root = _open_portable_root(project_dir)
        scanned_at = datetime.now(timezone.utc)
        scan = _PortableScan(self.config)
        scan.walk_path(root, root)
        return _finalize_scan(root, scanned_at, scan)
