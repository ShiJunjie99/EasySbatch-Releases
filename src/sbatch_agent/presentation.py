"""HTML presentation only. No filesystem I/O, inference or execution decisions."""

from dataclasses import dataclass, field
import json
import re
from pathlib import PurePosixPath
from .ui_formatting import format_args, format_duration, field_label


@dataclass(frozen=True)
class Badge:
    label: str
    tone: str = "secondary"
    description: str = ""

    @property
    def symbol(self):
        return {'success': '✓', 'danger': '×', 'warning': '⚠'}.get(self.tone, '●')

    @property
    def short_label(self):
        return self.label


PLACEHOLDERS = {"name": "scft-case01", "entrypoint": "run.py", "executable": "python3",
                "args": '--input input.json', "required_inputs": '["input.json"]',
                "memory_mib": "2048", "time_limit_seconds": "02:00:00",
                "project_dir": "/path/to/workspace/case01", "work_dir": "/path/to/workspace/case01"}


def project_summary(tree):
    """At most six high-value, actually observed files; no new detectors/I/O."""
    if tree is None:
        return ()
    def walk(node):
        if not node.is_directory and node.is_relevant:
            yield node
        for child in node.children:
            yield from walk(child)
    return tuple(sorted(walk(tree.root), key=lambda n: (n.relative_path.casefold(), n.relative_path))[:6])


def compact_review_values(prepared, values, catalog_entries):
    """Short display only; submitted controls/JobSpec retain exact full values."""
    result = dict(values)
    root = PurePosixPath(prepared.project_evidence.project_dir)
    def relative(value):
        path = PurePosixPath(value)
        return str(path.relative_to(root)) if path.is_relative_to(root) else value
    for key in ("entrypoint", "work_dir"):
        if result.get(key):
            result[key] = relative(result[key])
    for key in ("required_inputs", "args"):
        items = getattr(prepared.values, key)
        if items is not None:
            short = [relative(value) for value in items]
            result[key] = format_args(short) if key == 'args' else '、'.join(short)
    for key in ('environment_profile', 'launcher_profile'):
        profile = getattr(prepared.values, key)
        if profile is not None:
            result[key] = f'{profile.id} · {profile.version}'
    for key, entry in catalog_entries.items():
        result[key] = entry.display_name + (f" · {entry.version}" if entry.version else "")
    if result.get("executable"):
        result["executable"] = compact_executable(result["executable"])
    if result.get("run_type"):
        result["run_type"] = run_type_label(result["run_type"])
    if prepared.values.time_limit_seconds is not None:
        result['time_limit_seconds'] = format_duration(prepared.values.time_limit_seconds)
    for key, mode_key in (("memory_mib", "memory_mode"), ("time_limit_seconds", "walltime_mode")):
        if getattr(prepared.values, mode_key) == "cluster_default":
            result[key] = "集群默认"
        elif key == "memory_mib" and prepared.values.memory_mib is not None:
            result[key] = f"{prepared.values.memory_mib} MiB"
    return result


def status_badge(value):
    value = str(value or "UNKNOWN")
    tones = {"COMPLETED": "success", "RUNNING": "primary", "PENDING": "warning",
             "FAILED": "danger", "CANCELLED": "danger", "TIMEOUT": "danger",
             "OUT_OF_MEMORY": "danger", "NODE_FAIL": "danger", "SUBMIT_FAILED": "danger",
             "SUBMITTED": "primary", "SUBMITTING": "warning", "SUBMISSION_UNKNOWN": "warning",
             "READY_TO_SUBMIT": "success", "NEEDS_INPUT": "warning"}
    labels = {"COMPLETED": "已完成", "RUNNING": "运行中", "PENDING": "排队中",
              "FAILED": "运行失败", "CANCELLED": "已取消", "TIMEOUT": "运行超时",
              "OUT_OF_MEMORY": "内存不足", "NODE_FAIL": "节点故障", "SUBMIT_FAILED": "提交失败",
              "SUBMITTED": "已提交", "SUBMITTING": "提交中", "SUBMISSION_UNKNOWN": "提交结果未知",
              "READY_TO_SUBMIT": "可以提交", "NEEDS_INPUT": "需要确认", "SCRIPT_RENDERED": "待确认提交",
              "UNKNOWN": "状态未知", "尚未查询": "尚未查询", "UP": "可用", "DOWN": "不可用",
              "IDLE": "空闲", "ALLOCATED": "已分配", "MIXED": "部分占用", "DRAIN": "停止分配",
              "DRAINING": "正在排空", "COMPLETING": "收尾中", "CONFIGURING": "配置中",
              "SUSPENDED": "已暂停", "PREEMPTED": "已被抢占"}
    # Node flags (e.g. IDLE+DRAIN) are kept raw in the tooltip, not the label.
    parts = re.split(r'[+*~#%$@!]', value.upper())
    state = next((p for p in ('DOWN', 'DRAIN', 'DRAINING', 'FAILED') if p in parts), parts[0])
    return Badge(labels.get(value, labels.get(state, "状态未知")), tones.get(state, "secondary"), value)


def error_display(message):
    text = str(message)
    if text.startswith('校验位置：') or re.search(r'\b[a-z]+_[a-z_]+\b', text) or not re.search('[\u4e00-\u9fff]', text):
        return '配置未通过校验，请展开详情核对。', text
    return text, None


def field_source_badge(value):
    return {
        "AI_DIRECT": Badge("有明确依据", "primary", "AI 从项目证据中提取；不代表运行验证。"),
        "AI_INFERRED": Badge("系统推断", "secondary", "AI 推断，尚未验证，请核对。"),
        "PROJECT_EVIDENCE": Badge("项目依据", "primary", "来自项目文件；不代表运行验证。"),
        "SERVER_CATALOG": Badge("软件目录", "primary", "确定性匹配；可信状态以条目复核记录为准。"),
        "ENVIRONMENT_RESOLVER": Badge("已自动匹配", "primary", "由已登记环境确定性匹配。"),
        "RESOURCE_RECOMMENDER": Badge("已推荐", "primary", "基于集群快照；不保证立即运行。"),
        "RESOURCE_POLICY_RECOMMENDATION": Badge("智能推荐", "primary", "来自与当前任务匹配的明确配置；不是 AI 数值猜测。"),
        "CLUSTER_DEFAULT": Badge("集群默认", "secondary", "用户选择省略资源声明，由 Slurm / QOS / 账户策略决定。"),
        "USER": Badge("用户指定", "secondary", "用户明确选择。"),
        "PROFILE_DEFAULT": Badge("默认值", "secondary", "已登记配置的默认值。"),
        "SYSTEM_DEFAULT": Badge("默认值", "secondary", "系统默认值，不是项目证据。"),
        "DIRECT": Badge("有明确依据", "primary"),
        "INFERRED": Badge("系统推断", "secondary"),
        "UNRESOLVED": Badge("需要确认", "warning"),
    }.get(str(value), Badge("来源未知"))


def verification_label(value):
    return {"VERIFIED": "已复核", "DOCUMENTED": "文档记录", "INFERRED": "推断信息",
            "UNVERIFIED": "尚未复核"}.get(str(value), "尚未复核")


def file_tag_label(value):
    return {"Entrypoint": "入口", "Input": "输入", "Evidence": "依据", "SBATCH": "提交脚本",
            "Build": "构建", "Environment": "环境", "Skipped": "已跳过"}.get(value, "文件")


def run_type_label(value):
    return {"python": "Python 脚本", "compiled": "编译后运行", "installed": "已安装软件"}.get(value, "待确认")


def preference_label(value):
    return {"FASTEST_AVAILABLE": "空闲资源优先", "BALANCED": "综合权衡",
            "RESOURCE_EFFICIENT": "资源需求优先"}.get(str(value), "未指定")


def queue_reason_label(value):
    if not value or value == "None":
        return "—"
    return {"Priority": "等待调度优先级", "Resources": "等待资源", "Dependency": "等待依赖任务",
            "BeginTime": "等待指定开始时间", "JobHeldUser": "用户暂停", "JobHeldAdmin": "管理员暂停",
            "PartitionTimeLimit": "超出分区时限", "ReqNodeNotAvail": "所需节点不可用"}.get(value, "查看详细原因")


def compact_executable(value):
    return PurePosixPath(value).name if PurePosixPath(value).is_absolute() else value


def prepare_failure_copy(code):
    """Allowlisted display copy only: retry policy/status/code remain untouched."""
    return {
        "AI_RELAY_UNAVAILABLE": ("AI 连接暂不可用", "请联系维护者检查连接后，再试一次分析。"),
        "AI_PROVIDER_TIMEOUT": ("AI 分析超时", "未能在限定时间内完成，可稍后重试分析。"),
        "AI_RATE_LIMITED": ("AI 服务请求受限", "请稍后重试，或联系维护者检查服务额度。"),
        "AI_PROVIDER_UNAVAILABLE": ("AI 服务暂不可用", "本次分析未完成，可继续手动配置。"),
        "AI_CONFIGURATION_ERROR": ("AI 尚未就绪", "请联系维护者检查模型配置与访问凭据。"),
        "AI_AUTHENTICATION_FAILED": ("AI 服务认证失败", "请联系维护者检查访问权限。"),
        "AI_TLS_ERROR": ("AI 安全连接失败", "证书校验未通过，请联系维护者检查。"),
        "AI_OUTPUT_INVALID": ("AI 返回格式不符合要求", "结果未被采用，请手动核对配置。"),
        "AI_OUTPUT_REJECTED": ("AI 分析未通过校验", "结果与项目依据或运行规则冲突，请手动核对配置。"),
        "CATALOG_UNAVAILABLE": ("服务器软件目录不可用", "请联系维护者检查；仍可手动选择已登记的有效环境。"),
        "CLUSTER_UNAVAILABLE": ("集群信息暂不可用", "请手动补齐资源需求。"),
        "RECOMMENDATION_UNAVAILABLE": ("资源推荐暂不可用", "请手动补齐资源需求。"),
        "PREPARE_INPUT_INVALID": ("请检查工作目录和任务描述", "请选择可访问的目录，并填写任务描述。"),
    }.get(str(code), ("任务准备未完成", "请将排障信息中的请求编号提供给维护者。"))


@dataclass
class FileTreeNode:
    name: str
    relative_path: str
    is_directory: bool
    children: list["FileTreeNode"] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    is_relevant: bool = False
    is_skipped: bool = False
    file_count: int = 0  # Only observed files; skipped directory contents are unknown.
    omitted_files: int = 0
    expanded: bool = False


@dataclass(frozen=True)
class FileTreeViewModel:
    root: FileTreeNode
    rendered_nodes: int
    omitted_files: int
    limit: int


def build_file_tree(evidence, *, max_nodes=240):
    """Bound rendered nodes (including directories), prioritizing relevant paths.

    Evidence is already scanner-bounded. Building a small Python index is fine;
    rendering thousands of collapsed DOM nodes is not. Never resolve/read paths.
    """
    limit = max(1, min(max_nodes, 400))
    root = FileTreeNode(PurePosixPath(evidence.project_dir).name or "project", ".", True, expanded=True)
    nodes = {".": root}

    def safe_parts(path):
        p = PurePosixPath(path)
        return p.parts if not p.is_absolute() and ".." not in p.parts and len(p.parts) <= 16 else ()

    def add(path, directory=False):
        parts = safe_parts(path)
        if not parts:
            return root if path == "." else None
        parent = root
        for index, name in enumerate(parts):
            key = "/".join(parts[:index + 1])
            if key not in nodes:
                node = FileTreeNode(name, key, directory or index < len(parts) - 1)
                nodes[key] = node
                parent.children.append(node)
            parent = nodes[key]
        return parent

    for item in evidence.files:
        node = add(item.path)
        if node and not node.is_directory:
            node.is_skipped = item.status == "skipped"
            if node.is_skipped:
                node.tags.append("Skipped")
    for path in evidence.skipped_directories:
        node = add(path, True)
        if node:
            node.is_skipped = True
            if "Skipped" not in node.tags:
                node.tags.append("Skipped")

    def tag(path, label):
        node = nodes.get(path)
        if node and not node.is_directory and label not in node.tags:
            node.tags.append(label)
            node.is_relevant = True

    by_id = {item.id: item for item in evidence.evidence_items}
    # Evidence sources are relevant, but a README mentioning an entrypoint is
    # not itself tagged as the entrypoint or the input.
    for item in evidence.evidence_items:
        tag(item.source_path, "Evidence")
    for group, label in (("entrypoint_candidates", "Entrypoint"), ("input_candidates", "Input"),
                         ("existing_sbatch_scripts", "SBATCH"), ("build_candidates", "Build"),
                         ("environment_hints", "Environment")):
        for candidate in getattr(evidence, group):
            tag(candidate.value, label)
            if label in {"SBATCH", "Build", "Environment"}:
                for ref in candidate.evidence_ids:
                    if ref in by_id:
                        tag(by_id[ref].source_path, label)

    def aggregate(node):
        if not node.is_directory:
            node.file_count = 1
            return
        for child in node.children:
            aggregate(child)
        node.file_count = sum(c.file_count for c in node.children)
        node.is_relevant = any(c.is_relevant for c in node.children)
        node.expanded = (node is root or node.is_relevant) and not node.is_skipped
        node.children.sort(key=lambda c: (not c.is_relevant, not c.is_directory, c.name.casefold()))

    aggregate(root)
    root.expanded = True
    # Select globally before pruning: a large src/ directory must not consume
    # the budget before a relevant README or input in another branch. Ancestors
    # of every relevant node are relevant too and are selected at lower depths.
    candidates = sorted((n for n in nodes.values() if n is not root),
                        key=lambda n: (not n.is_relevant, n.relative_path.count('/'),
                                       not n.is_directory, n.relative_path.casefold()))
    selected = {'.', *(n.relative_path for n in candidates[:limit - 1])}

    def prune(node):
        kept = []
        for child in node.children:
            if child.relative_path in selected:
                prune(child)
                kept.append(child)
            else:
                node.omitted_files += child.file_count
        node.children = kept

    prune(root)

    def omitted(node):
        return node.omitted_files + sum(omitted(c) for c in node.children)

    return FileTreeViewModel(root, len(selected), omitted(root), limit)


REVIEW_GROUPS = (
    ("任务", ("name", "run_type", "entrypoint", "required_inputs", "args")),
    ("软件与环境", ("software_id", "executable", "environment_profile")),
    ("计算资源", ("partition", "nodes", "ntasks", "cpus_per_task", "gpu_count", "gpu_type", "memory_mib", "time_limit_seconds", "account", "qos")),
    ("其他配置", ("work_dir", "launcher_profile", "prepare_steps", "stdout", "stderr")),
)


def catalog_details(prepared, catalog):
    """Show verification of the actual selected entries, not guessed trust."""
    if not catalog:
        return {}
    return {key: entry for key, entry in (
        ("software_id", catalog.software_by_id(prepared.values.software_id)),
        ("environment_profile", catalog.environment(prepared.values.environment_profile)),
    ) if entry is not None}


def snapshot_label(snapshot=None, report=None):
    """Retain the explicit offline marker; never infer liveness from a name."""
    warnings = (*getattr(snapshot, "warnings", ()), *getattr(report, "warnings", ()))
    offline = any("TEST/OFFLINE" in w for w in warnings)
    captured = getattr(report, "snapshot_captured_at", None) or getattr(snapshot, "captured_at", None)
    prefix = "基于测试 / 离线集群快照（TEST/OFFLINE）· 采集于" if offline else "基于集群快照 · 采集于"
    return f"{prefix} {captured.isoformat()}" if captured else "集群快照不可用"
