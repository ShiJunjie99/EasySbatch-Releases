"""Single-level, metadata-only workspace navigation; not a project scanner.

All directory opens are descriptor-relative and refuse symlinks, including the
configured root's ancestors. No file contents, recursive enumeration or commands.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
import os
from pathlib import Path, PurePosixPath
import stat


class FolderAccessError(ValueError):
    def __init__(self, message="无法访问此文件夹，请在工作区内选择。", status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SelectedFolderViewModel:
    display_path: str
    relative_path: str
    absolute_path: Path = field(repr=False)


@dataclass(frozen=True)
class DirectoryEntryViewModel:
    name: str
    relative_path: str
    is_dir: bool
    is_symlink: bool
    is_accessible: bool


@dataclass(frozen=True)
class FolderListingViewModel:
    current: SelectedFolderViewModel
    entries: tuple[DirectoryEntryViewModel, ...]
    breadcrumbs: tuple[tuple[str, str], ...]
    parent: str | None
    limit: int
    truncated: bool
    enumeration_limited: bool


class WorkspaceBrowser:
    """Limits bound both DOM and filesystem work. Hidden names are not exposed.

Above the enumeration bound there is deliberately no filesystem-order sample:
use the advanced exact path to narrow the directory, or select the current one.
"""
    ENUMERATION_LIMIT = 2000
    FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def __init__(self, root: Path, *, max_entries_per_directory=200):
        self.root = Path(root)
        if (not self.root.is_absolute() or self.root == Path("/")
                or ".." in self.root.parts or type(max_entries_per_directory) is not int
                or not 1 <= max_entries_per_directory <= 500):
            raise ValueError("Invalid workspace root or directory entry limit")
        self.limit = max_entries_per_directory
        self._identity = None
        try:
            with self._open(".") as fd:
                info = os.fstat(fd)
                self._identity = (info.st_dev, info.st_ino)
        except FolderAccessError:
            raise ValueError("Workspace root must be an accessible directory without symlinks") from None

    @staticmethod
    def _parts(relative):
        if relative == ".":
            return ()
        if (not isinstance(relative, str) or not relative or len(relative) > 2048
                or relative.startswith("/") or "\\" in relative or "%" in relative
                or not relative.isprintable()):
            raise FolderAccessError()
        parts = relative.split("/")
        if len(parts) > 32 or any(not p or p.startswith(".") for p in parts):
            raise FolderAccessError()
        return tuple(parts)

    @contextmanager
    def _open(self, relative):
        parts = self._parts(relative)
        fd = None
        try:
            fd = os.open("/", self.FLAGS)
            for part in self.root.parts[1:]:
                child = os.open(part, self.FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if self._identity is not None and (info.st_dev, info.st_ino) != self._identity:
                raise FolderAccessError()
            for part in parts:
                child = os.open(part, self.FLAGS, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                raise FolderAccessError("没有权限打开此文件夹。", 403) from None
            raise FolderAccessError() from None
        finally:
            if fd is not None:
                os.close(fd)

    def _selected(self, relative):
        parts = self._parts(relative)
        return SelectedFolderViewModel(" / ".join(parts) or "工作区", relative, self.root.joinpath(*parts))

    def select(self, relative):
        with self._open(relative):
            return self._selected(relative)

    def from_absolute(self, value):
        """Advanced input is also untrusted, and follows the same root boundary."""
        try:
            path = Path(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError
            relative = path.relative_to(self.root).as_posix()
        except (ValueError, TypeError):
            raise FolderAccessError() from None
        return self.select(relative)

    def browse(self, relative="."):
        entries = []
        enumeration_limited = False
        with self._open(relative) as fd:
            with os.scandir(fd) as stream:
                for count, entry in enumerate(stream):
                    if count >= self.ENUMERATION_LIMIT:
                        entries.clear()
                        enumeration_limited = True
                        break
                    child_path = str(PurePosixPath(relative) / entry.name)
                    try:
                        self._parts(child_path)
                    except FolderAccessError:
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue  # disappeared/unreadable metadata, never follow
                    link = stat.S_ISLNK(info.st_mode)
                    directory = stat.S_ISDIR(info.st_mode)
                    if not (link or directory or stat.S_ISREG(info.st_mode)):
                        continue
                    accessible = directory and os.access(entry.name, os.R_OK | os.X_OK,
                                                         dir_fd=fd, follow_symlinks=False)
                    entries.append(DirectoryEntryViewModel(entry.name, child_path, directory, link, accessible))
        entries.sort(key=lambda e: (not e.is_dir, e.name.casefold(), e.name))
        parts = self._parts(relative)
        crumbs = (("工作区", "."),) + tuple((p, "/".join(parts[:i + 1])) for i, p in enumerate(parts))
        return FolderListingViewModel(self._selected(relative), tuple(entries[:self.limit]), crumbs,
                                      str(PurePosixPath(relative).parent) if parts else None,
                                      self.limit, len(entries) > self.limit, enumeration_limited)
