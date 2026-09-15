"""Bounded static text/AST detectors. Project text is data, never instructions."""

import ast
import configparser
from dataclasses import dataclass
from functools import cached_property
from pathlib import PurePosixPath
import re
import shlex
import tomllib

from .scanner_models import EvidenceItem, EvidenceLevel, ProjectCandidate, ScanConfig


# Lower is earlier. These are source priorities, not confidence probabilities.
PRIORITY = {"readme": 10, "configured": 20, "sbatch": 30, "shell": 35,
            "main_guard": 40, "metadata": 50, "filename": 60}
SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cu"}
SHELL_SUFFIXES = {".sh", ".bash", ".sbatch", ".slurm"}
INPUT_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".in", ".dat",
                  ".txt", ".gro", ".top", ".mdp", ".data", ".xyz", ".pdb", ".itp"}
INPUT_DIRS = {"example", "examples", "input", "inputs", "config", "configs", "case", "cases"}
ENV_NAMES = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "environment.yml",
             "environment.yaml", "pipfile", "poetry.lock"}
SBATCH_FIELDS = {"partition", "nodes", "ntasks", "cpus-per-task", "mem", "time", "gres",
                 "gpus", "account", "qos", "mem-per-cpu", "mem-per-gpu", "chdir"}
PARALLEL_PATTERNS = {
    "mpi": re.compile(r"\b(?:MPI|mpi4py|mpirun|mpiexec)\b", re.I),
    "launcher": re.compile(r"\bsrun\b"),
    "threads": re.compile(r"\b(?:OpenMP|OMP_NUM_THREADS)\b", re.I),
    "gpu": re.compile(r"\b(?:CUDA|GPU|NVIDIA|cupy)\b|\b(?:torch|numba)\.cuda\b", re.I),
}


def is_readme(path: str) -> bool:
    return bool(re.match(r"^(readme|usage|install)(?:[._-].*)?$", PurePosixPath(path).name, re.I))


def file_priority(path: str):
    name = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    category = (0 if is_readme(path) else 1 if name in ENV_NAMES else
                2 if suffix in SHELL_SUFFIXES else 3 if name in {"cmakelists.txt", "makefile"}
                else 4 if suffix == ".py" or suffix in SOURCE_SUFFIXES else 5)
    return category, path


def relevant_text(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    suffix = PurePosixPath(path).suffix.lower()
    # Other docs, logs and lock bodies are not indiscriminately read. Extensionless
    # small files can carry a shebang and therefore need a bounded text inspection.
    return (is_readme(path) or name in ENV_NAMES or name in {"cmakelists.txt", "makefile"}
            or suffix in SHELL_SUFFIXES | INPUT_SUFFIXES | SOURCE_SUFFIXES | {".py"} or not suffix)


@dataclass(frozen=True)
class Document:
    path: str
    text: str

    @cached_property
    def lines(self):
        # Match physical source lines; str.splitlines also splits Unicode
        # separators inside literals, which would corrupt AST line references.
        lines = self.text.split("\n") if self.text else []
        if lines and lines[-1] == "":
            lines.pop()
        return tuple(lines)


class EvidenceBuilder:
    """Per-scan collector. Deduplication and all ordering are explicit."""

    def __init__(self, config: ScanConfig):
        self.config = config
        self.items = {}
        self.groups = {}
        self.warnings = set()
        self.limits = set()
        self.ambiguities = set()
        self.references = []  # (relative input path, evidence id, priority)

    def limit(self, name):
        self.limits.add(name)
        self.warnings.add(f"达到扫描限制 {name}；结果已截断，未扫描部分不能视为不存在。")

    def emit(self, group, kind, value, doc, line=None, end=None, *, description,
             level=EvidenceLevel.DIRECT, priority=PRIORITY["metadata"]):
        lines = doc.lines
        end = end or line
        snippet = "\n".join(lines[line - 1:end]) if line else ""
        snippet = snippet[:self.config.max_snippet_chars]
        key = (doc.path, line, end, kind, value, description, snippet, level)
        if key not in self.items:
            if len(self.items) >= self.config.max_evidence_items:
                self.limit("max_evidence_items")
                return None
            self.items[key] = EvidenceItem(
                id=f"e{len(self.items) + 1:05d}", kind=kind, source_path=doc.path,
                line_start=line, line_end=end, snippet=snippet, description=description, level=level,
                value=value[:self.config.max_snippet_chars],
            )
        item = self.items[key]
        if group:
            # A pathological one-line command must not bypass snippet/output bounds.
            if len(value) > self.config.max_snippet_chars:
                value = value[:self.config.max_snippet_chars]
                self.warnings.add(f"{doc.path}: 候选文本过长，展示已截断；不能当作完整运行命令。")
                level = EvidenceLevel.UNKNOWN
            self.add_candidate(group, kind, value, level, priority, item.id)
        return item.id

    def add_candidate(self, group, kind, value, level, priority, evidence_id):
        target = self.groups.setdefault(group, {})
        key = kind, value
        old = target.get(key)
        ids = {evidence_id}
        if old:
            ids.update(old.evidence_ids)
            priority = min(priority, old.priority)
            levels = (EvidenceLevel.DIRECT, EvidenceLevel.INFERRED, EvidenceLevel.UNKNOWN)
            level = min((level, old.level), key=levels.index)
        target[key] = ProjectCandidate(kind=kind, value=value, level=level, priority=priority,
                                       evidence_ids=tuple(sorted(ids)))

    def candidates(self):
        return {group: tuple(sorted(items.values(), key=lambda c: (c.priority, c.value, c.kind)))
                for group, items in sorted(self.groups.items())}

    def finalize_inputs(self, documents):
        # Only files already safely read by the filesystem scanner can become
        # referenced input candidates. Never open a path found in project text.
        by_path = {doc.path: doc for doc in documents}
        for path, eid, priority in self.references:
            if path in by_path and PurePosixPath(path).suffix.lower() in INPUT_SUFFIXES:
                doc = by_path[path]
                self.emit("input_candidates", "input", path, doc, 1 if doc.lines else None,
                          description="已读取的小型文本文件被说明或命令引用；是否必需仍待确认。",
                          priority=priority)
                self.add_candidate("input_candidates", "input", path, EvidenceLevel.DIRECT, priority, eid)


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _main_guard(node):
    if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    pair = node.left, node.comparators[0]
    return any(isinstance(a, ast.Name) and a.id == "__name__" and
               isinstance(b, ast.Constant) and b.value == "__main__" for a, b in (pair, pair[::-1]))


def detect_python(doc, out):
    lines = doc.lines
    out.emit("project_type_candidates", "run_type", "python", doc, 1 if lines else None,
             description="发现 Python 源文件；不表示它是唯一主运行类型。")
    if PurePosixPath(doc.path).name in {"main.py", "run.py", "simulate.py"}:
        out.emit("entrypoint_candidates", "python_script", doc.path, doc,
                 description="文件名符合常见入口约定，尚未确认。", level=EvidenceLevel.INFERRED,
                 priority=PRIORITY["filename"])
    try:
        tree = ast.parse(doc.text, filename=doc.path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        out.warnings.add(f"{doc.path}: Python 静态语法解析失败（{type(exc).__name__}）；其他文件继续扫描。")
        return
    nodes = list(ast.walk(tree))
    imports = set()
    aliases = {}
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for module in modules:
                imports.add(module.split(".")[0])
                out.emit("environment_hints", "python_import", module, doc, node.lineno, node.end_lineno,
                         description="源码静态 import 引用；未导入模块，也未确认服务器已安装。")
            for alias in node.names:
                full = alias.name if isinstance(node, ast.Import) else f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = full
        if isinstance(node, ast.If) and _main_guard(node.test):
            out.emit("entrypoint_candidates", "python_script", doc.path, doc, node.lineno,
                     getattr(node.test, "end_lineno", node.lineno),
                     description="存在 __main__ 判断；可作为入口候选，未执行验证。",
                     level=EvidenceLevel.INFERRED, priority=PRIORITY["main_guard"])
    for framework in sorted(imports & {"argparse", "click", "typer"}):
        node = next(n for n in nodes if isinstance(n, (ast.Import, ast.ImportFrom)) and
                    (any(a.name.split('.')[0] == framework for a in n.names) if isinstance(n, ast.Import)
                     else (n.module or '').split('.')[0] == framework))
        out.emit("cli_hints", "cli_framework", framework, doc, node.lineno, node.end_lineno,
                 description="发现 CLI 解析库引用；不执行参数解析器。")
    for node in nodes:
        if isinstance(node, ast.Call) and imports & {"argparse", "click", "typer"}:
            name = _dotted(node.func)
            root, _, rest = name.partition(".")
            name = aliases.get(root, root) + ("." + rest if rest else "")
            if name.endswith((".add_argument", ".option", ".Option", ".Argument")):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.fullmatch(r"--[\w-]+", arg.value):
                        out.emit("cli_hints", "cli_option", arg.value, doc, node.lineno, node.end_lineno,
                                 description="静态 CLI 声明中的显式选项；必填性与完整语义未推断。")
        if isinstance(node, (ast.Attribute, ast.Name)):
            name = _dotted(node)
            root, _, rest = name.partition(".")
            full = aliases.get(root, root) + ("." + rest if rest else "")
            if full in {"torch.cuda", "numba.cuda"}:
                out.emit("parallelism_hints", "gpu", full, doc, node.lineno, node.end_lineno,
                         description="源码引用 CUDA API；这是相关线索，不表示 GPU 必需或数量。")
    if PurePosixPath(doc.path).name.lower() == "setup.py":
        for node in nodes:
            if isinstance(node, ast.Call) and _dotted(node.func) in {"setup", "setuptools.setup"}:
                for keyword in node.keywords:
                    if keyword.arg == "entry_points":
                        try:
                            mapping = ast.literal_eval(keyword.value)
                        except (ValueError, TypeError, SyntaxError, RecursionError):
                            out.ambiguities.add(f"{doc.path}: 动态 setup.py entry_points 不支持静态求值。")
                            continue
                        values = mapping.get("console_scripts", []) if isinstance(mapping, dict) else []
                        if isinstance(values, list) and all(isinstance(v, str) for v in values):
                            for value in values:
                                _console_entry(value, doc, keyword.value.lineno, keyword.value.end_lineno, out)


def _console_entry(value, doc, line, end, out):
    if re.fullmatch(r"\s*[\w.-]+\s*=\s*[\w.]+:[\w.]+\s*", value):
        out.emit("entrypoint_candidates", "console_script", value.strip(), doc, line, end,
                 description="项目配置显式登记 console script；目标是否存在及安装状态未确认。",
                 priority=PRIORITY["configured"])
    else:
        out.ambiguities.add(f"{doc.path}: 不支持的 console script 表达式，需人工核对。")


def _section_lines(doc, section):
    """Locate simple section syntax only; complex syntax uses an honest broad citation."""
    active = False
    selected = []
    for i, text in enumerate(doc.lines, 1):
        if re.match(r"\s*\[", text):
            active = bool(re.fullmatch(r"\s*\[" + re.escape(section) + r"\]\s*(?:#.*)?", text))
        elif active:
            selected.append((i, text))
    return selected


def detect_environment(doc, out):
    name = PurePosixPath(doc.path).name.lower()
    out.emit("environment_hints", "dependency_metadata", doc.path, doc, 1 if doc.lines else None,
             description="发现依赖/环境配置文件；只读线索，不自动匹配 EnvironmentProfile。")
    out.emit("project_type_candidates", "run_type", "python", doc, 1 if doc.lines else None,
             description="发现 Python/Conda 相关配置；项目主类型仍需结合其他证据。",
             level=EvidenceLevel.INFERRED)
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(doc.text)
        except (tomllib.TOMLDecodeError, RecursionError):
            out.warnings.add(f"{doc.path}: TOML 解析失败；保留文件存在证据。")
            return
        project = data.get("project", {})
        scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
        if isinstance(scripts, dict):
            for command, target in sorted(scripts.items()):
                line = next((i for i, text in _section_lines(doc, "project.scripts")
                             if re.match(r"\s*" + re.escape(command) + r"\s*=", text)
                             or re.match(r'''\s*["']''' + re.escape(command) + r'''["']\s*=''', text)), None)
                # Complex TOML quoting/inline tables use a whole-file range; it
                # is an honest (bounded snippet) citation, not an invented line.
                _console_entry(f"{command} = {target}", doc, line or 1, line or len(doc.lines), out)
    elif name == "setup.cfg":
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(doc.text)
        except configparser.Error:
            out.warnings.add(f"{doc.path}: setup.cfg 解析失败；保留文件存在证据。")
            return
        value = parser.get("options.entry_points", "console_scripts", fallback="")
        for entry in value.splitlines():
            if entry.strip():
                line = next((i for i, text in _section_lines(doc, "options.entry_points")
                             if text.strip() == entry.strip() or re.fullmatch(
                                 r"\s*console_scripts\s*=\s*" + re.escape(entry.strip()) + r"\s*", text)), None)
                _console_entry(entry, doc, line or 1, line or len(doc.lines), out)
    elif name == "requirements.txt":
        for i, line in enumerate(doc.lines, 1):
            match = re.match(r"\s*([A-Za-z0-9][\w.-]*)(?:\[.*?\])?(?:\s*[<>=!~;#]|\s*$)", line)
            if match:
                out.emit("environment_hints", "python_dependency", match[1], doc, i,
                         description="requirements 中的依赖名称；未安装、未解析环境标记或版本求解。")


def detect_build(doc, out):
    name = PurePosixPath(doc.path).name.lower()
    out.emit("project_type_candidates", "run_type", "compiled", doc, 1 if doc.lines else None,
             description="发现 C/C++ 源码或构建文件；只记录构建类型候选。")
    if name in {"cmakelists.txt", "makefile"}:
        out.emit("build_candidates", "build_file", "cmake" if name == "cmakelists.txt" else "make", doc,
                 1 if doc.lines else None, description="发现构建文件；没有运行构建或 dry-run。")
    if name == "cmakelists.txt":
        # Mask comments while preserving offsets/lines. Bracket comments and
        # variable/generator expressions are intentionally outside this parser.
        if re.search(r"#\[=*\[", doc.text):
            out.ambiguities.add(f"{doc.path}: CMake bracket comments 不支持；未解析 executable target。")
            return
        cleaned = re.sub(r"#[^\n]*", lambda m: " " * len(m[0]), doc.text)
        for match in re.finditer(r"\badd_executable\s*\(([^)]*)\)", cleaned, re.I):
            line = cleaned.count("\n", 0, match.start()) + 1
            end = cleaned.count("\n", 0, match.end()) + 1
            tokens = match[1].split()
            if not tokens:
                continue
            target = tokens[0].strip('"')
            if re.fullmatch(r"[A-Za-z_][\w.+-]*", target) and not any(t in {"ALIAS", "IMPORTED"} for t in tokens[1:]):
                out.emit("executable_candidates", "cmake_target", target, doc, line, end,
                         description="add_executable 的字面 target；不是已存在或已验证的最终二进制路径。")
            else:
                out.emit("build_candidates", "unresolved_target", match[0], doc, line, end,
                         description="变量、ALIAS 或 IMPORTED target 无法确定本项目输出。", level=EvidenceLevel.UNKNOWN)
                out.ambiguities.add(f"{doc.path}:{line}: CMake target 需要人工解析。")


def _tokens(line):
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return []


def _remember_paths(tokens, doc, eid, priority, out):
    if not eid:
        return
    for token in tokens:
        value = token.split("=", 1)[-1].strip('`"')
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "$" in value:
            continue
        if path.suffix.lower() in INPUT_SUFFIXES:
            # Project-root and source-relative interpretations are candidates;
            # only safely read actual files will survive finalize_inputs().
            for candidate in sorted({str(path), str(PurePosixPath(doc.path).parent / path)}):
                out.references.append((candidate, eid, priority))


def detect_commands(doc, out, *, readme=False, shell=False, sbatch=False):
    priority = PRIORITY["readme" if readme else "sbatch" if sbatch else "shell"]
    fence = None
    shell_code = False
    for i, original in enumerate(doc.lines, 1):
        line = original.strip()
        if line.startswith(("```", "~~~")):
            if fence is None:
                fence = line[:3]
                shell_code = line[3:].strip().lower() in {"", "bash", "sh", "shell", "console", "shell-session", "text"}
            elif line.startswith(fence):
                fence = None
                shell_code = False
            continue
        if readme and fence is not None and not shell_code:
            continue
        if readme and re.fullmatch(r"requires [1-9][0-9]* (?:MiB (?:of )?memory per node|seconds (?:of )?walltime)\.?", line, re.I):
            out.emit(None, "resource_instruction", line, doc, i,
                     description="运行说明中的明确资源值；仍需匹配当前命令与布局。")
        if line.startswith("$ "):
            line = line[2:]
        if line.startswith("#SBATCH"):
            tokens = _tokens(line[len("#SBATCH"):])
            j = 0
            while j < len(tokens):
                flag, sep, value = tokens[j].partition("=")
                if flag.startswith("--") and flag[2:] in SBATCH_FIELDS:
                    if not sep and j + 1 < len(tokens) and not tokens[j + 1].startswith("-"):
                        j += 1
                        value = tokens[j]
                    out.emit(None, f"sbatch.{flag[2:]}", value, doc, i,
                             description=f"已有脚本资源声明 {flag}={value[:out.config.max_snippet_chars]}；不是集群验证事实。")
                j += 1
            if not tokens:
                out.warnings.add(f"{doc.path}:{i}: SBATCH 行为空或无法静态分词。")
            continue
        if not line or line.startswith("#"):
            continue
        tokens = _tokens(line)
        if not tokens:
            continue
        while tokens and re.match(r"^[A-Za-z_]\w*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        cmd = tokens[0]
        basename = PurePosixPath(cmd).name
        if basename in {"module", "source", ".", "conda"}:
            is_load = ((cmd == "module" and tokens[1:2] == ["load"]) or
                       cmd in {"source", "."} or (cmd == "conda" and tokens[1:2] == ["activate"]))
            if is_load:
                out.emit("environment_hints", "environment_command", line, doc, i,
                         description="文本中的环境加载命令；未执行，未创建或匹配环境配置。", priority=priority)
                if cmd == "module":
                    for software in tokens[2:]:
                        if not software.startswith("-"):
                            out.emit("installed_software_hints", "module_reference", software, doc, i,
                                     description="项目脚本/说明引用 module；服务器是否安装尚未确认。", priority=priority)
            continue
        is_python = bool(re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", basename))
        launcher = basename in {"srun", "mpirun", "mpiexec"}
        build = basename in {"make", "cmake"}
        obvious = is_python or launcher or build or cmd.startswith("./") or basename in {"gmx", "gmx_mpi"}
        # Generic standalone program invocations are evidence only in a shell
        # or README code block. Prose cannot become a guessed executable name.
        generic = (shell or shell_code) and bool(re.fullmatch(r"[\w./+-]+", cmd)) and basename not in {
            "echo", "printf", "test", "set", "export", "cd", "exit", "if", "then", "fi", "for", "do", "done",
            "else", "elif", "while", "function", "return", "true", "false", "read", "cat", "ls", "pwd",
            "mkdir", "rm", "mv", "cp", "curl", "wget", "pip", "pip3", "sudo", "su", "sbatch", "scancel",
        }
        if obvious or generic:
            group = "build_candidates" if build else "existing_run_commands"
            eid = out.emit(group, "build_command" if build else "command_text", line, doc, i,
                           description="项目文本中的命令候选；保留原文，不执行，也不保证适用于当前环境。", priority=priority)
            _remember_paths(tokens[1:], doc, eid, priority, out)
            if is_python:
                out.emit("project_type_candidates", "run_type", "python", doc, i,
                         description="说明或脚本显式调用 Python。", priority=priority)
                target, kind = None, "python_script"
                if "-m" in tokens and tokens.index("-m") + 1 < len(tokens):
                    target, kind = tokens[tokens.index("-m") + 1], "python_module"
                elif "-c" not in tokens:
                    target = next((t for t in tokens[1:] if t.endswith(".py") and not t.startswith("-")), None)
                if target:
                    out.emit("entrypoint_candidates", kind, target.removeprefix("./"), doc, i,
                             description="显式 Python 启动命令的入口；相对路径解释与目标存在性仍需核对。", priority=priority)
            elif not (build or launcher) and not cmd.startswith(("./", "/")):
                out.emit("installed_software_hints", "command_reference", basename, doc, i,
                         description="项目引用外部命令；未搜索服务器 PATH 或判断软件安装。", priority=priority)
                out.emit("project_type_candidates", "run_type", "installed", doc, i,
                         description="存在外部程序调用；可能是已有软件运行场景。", level=EvidenceLevel.INFERRED, priority=priority)
        elif readme:
            # Markdown/backtick path references outside command lines, without
            # interpreting arbitrary prose as executable instructions.
            refs = re.findall(r"`([^`]+)`|\]\(([^)]+)\)", line)
            for a, b in refs:
                value = a or b
                if PurePosixPath(value).suffix.lower() in INPUT_SUFFIXES:
                    eid = out.emit(None, "input_reference", value, doc, i,
                                   description="README 中的文件引用；只关联项目内已安全读取的文本。")
                    _remember_paths([value], doc, eid, priority, out)


def detect_document(doc: Document, out: EvidenceBuilder):
    path = PurePosixPath(doc.path)
    name, suffix = path.name.lower(), path.suffix.lower()
    readme = is_readme(doc.path)
    shell = suffix in SHELL_SUFFIXES or bool(re.match(r"^#!\s*(?:/bin/(?:ba)?sh|/usr/bin/(?:env\s+)?(?:ba)?sh)\b", doc.text))
    sbatch = suffix in {".sbatch", ".slurm"} or any(re.match(r"\s*#SBATCH\b", line) for line in doc.lines)
    if readme:
        out.emit(None, "readme", doc.path, doc, 1 if doc.lines else None,
                 description="项目使用说明，属于不可信文本证据，不能改变扫描权限。")
    if shell or sbatch:
        for group in ("existing_shell_scripts",) + (("existing_sbatch_scripts",) if sbatch else ()):
            out.emit(group, "existing_script", doc.path, doc, 1 if doc.lines else None,
                     description="发现已有脚本；可能过期或面向其他机器，未执行验证。")
        detect_commands(doc, out, shell=True, sbatch=sbatch)
    elif readme:
        detect_commands(doc, out, readme=True)
    if suffix == ".py":
        detect_python(doc, out)
    if name in ENV_NAMES:
        detect_environment(doc, out)
    if name in {"cmakelists.txt", "makefile"} or suffix in SOURCE_SUFFIXES:
        detect_build(doc, out)
    if suffix in INPUT_SUFFIXES and name not in ENV_NAMES and not readme and (
        set(path.parts[:-1]) & INPUT_DIRS or path.stem.lower() in INPUT_DIRS
        or suffix in {".gro", ".top", ".mdp", ".data", ".in", ".xyz", ".pdb"}
    ):
        out.emit("input_candidates", "input", doc.path, doc, 1 if doc.lines else None,
                 description="输入目录/文件名约定或科学配置扩展名中的小型文本；是否必需尚未确定。",
                 level=EvidenceLevel.INFERRED, priority=PRIORITY["filename"])
    if readme or shell or sbatch or suffix in {".py"} | SOURCE_SUFFIXES or name in ENV_NAMES:
        for i, line in enumerate(doc.lines, 1):
            for kind, pattern in PARALLEL_PATTERNS.items():
                if pattern.search(line):
                    out.emit("parallelism_hints", kind, kind, doc, i,
                             description="发现并行相关文字/API/命令；不表示必需，不推导资源数量或启动布局。")
