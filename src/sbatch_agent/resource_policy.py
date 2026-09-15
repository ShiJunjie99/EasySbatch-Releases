"""Deterministic resource values. No model, queue, file I/O or Slurm queries."""

import shlex

from .analyzer import _numeric_evidence
from .models import ResourceValueEvidence, ResourceValuePolicy, ResourceValueRecommendation


POLICY_FIELDS = {"memory_mib": ("memory_mode", "memory_policy"),
                 "time_limit_seconds": ("walltime_mode", "walltime_policy")}
MODE_LABELS = {"cluster_default": "集群默认", "recommended": "智能推荐", "explicit": "用户指定"}
UNAVAILABLE = {"memory_mib": "智能推荐暂不可用；请选择使用集群默认或手动指定。",
               "time_limit_seconds": "暂时没有足够依据推荐运行时限；请选择使用集群默认或手动指定。"}


def policy_data(values, recommendations):
    """Formal policy materialization: recommended without a candidate fails closed."""
    result = {}
    for key, (mode_key, policy_key) in POLICY_FIELDS.items():
        mode = getattr(values, mode_key, None) or "explicit"
        evidence = None
        if mode == "recommended":
            candidate = recommendations.get(key)
            if candidate is None or getattr(values, key) != candidate.value:
                raise ValueError(UNAVAILABLE[key])
            evidence = candidate.evidence
        result[policy_key] = ResourceValuePolicy(mode=mode, evidence=evidence).model_dump()
    return result


def _relative(token, root):
    prefix, sep, path = token.partition("=")
    if not (sep and prefix.startswith("-")):
        prefix, sep, path = "", "", token
    if root and path.startswith(root.rstrip("/") + "/"):
        path = path[len(root.rstrip("/")) + 1:]
    return prefix + sep + path.removeprefix("./")


def _command(executable, args, root):
    return tuple(_relative(a, root) for a in [executable, *args])


def _layout(values):
    return (values.nodes, values.ntasks, values.cpus_per_task,
            values.gpu_count, values.gpu_type)


def _rules(rules, values, root):
    selected = _command(values.executable, values.args, root)
    matches = []
    for rule in rules:
        gpu = rule.gpus
        if (rule.run_type == values.run_type and
                _command(rule.command.executable, rule.command.args, root) == selected and
                (rule.nodes, rule.ntasks, rule.cpus_per_task, gpu.count if gpu else 0,
                 gpu.gpu_type if gpu else None) == _layout(values)):
            matches.append(rule)
    result = {}
    for key in POLICY_FIELDS:
        relevant = [r for r in matches if getattr(r, key) is not None]
        quantities = {getattr(r, key) for r in relevant}
        if relevant:
            result[key] = None  # Conflicting rules block lower-priority fallback.
        if len(quantities) == 1:
            rule = relevant[0]
            evidence = rule.evidence.model_copy(update={"reason":
                f"{rule.evidence.reason} 复核范围：{rule.verification_scope}；复核时间：{rule.verified_at.isoformat()}。"})
            result[key] = ResourceValueRecommendation(value=quantities.pop(), evidence=evidence)
    return result


def _project(evidence, values):
    if evidence is None or evidence.limits_reached:
        return {}
    root = evidence.project_dir
    selected = _command(values.executable, values.args, root)
    by_source = {}
    for item in evidence.evidence_items:
        by_source.setdefault(item.source_path, []).append(item)
    found = {key: [] for key in POLICY_FIELDS}
    for source, items in sorted(by_source.items()):
        commands = [e for e in items if e.kind == "command_text"]
        try:
            tokens = [shlex.split(e.value or "") for e in commands]
        except ValueError:
            continue
        # A resource statement applies only to the single exact invocation in
        # its source, at the selected project-root working directory.
        if not tokens or any(tuple(_relative(t, root) for t in cmd) != selected for cmd in tokens):
            continue
        if values.work_dir != root or any(e.kind == "sbatch.chdir" for e in items):
            continue
        layout = {}
        invalid = False
        for key, kind, default in (("nodes", "nodes", 1), ("ntasks", "ntasks", 1),
                                   ("cpus_per_task", "cpus-per-task", 1), ("gpu_count", "gres", 0)):
            refs = [e for e in items if e.kind == "sbatch." + kind or (key == "gpu_count" and e.kind == "sbatch.gpus")]
            counts = {_numeric_evidence(key, e) for e in refs}
            if refs and (None in counts or len(counts) != 1):
                invalid = True
            layout[key] = next(iter(counts)) if counts else default
        if invalid or any(getattr(values, key) != val for key, val in layout.items()) or values.gpu_type:
            continue
        for key, kind in (("memory_mib", "sbatch.mem"), ("time_limit_seconds", "sbatch.time")):
            refs = [e for e in items if e.kind == kind or
                    (e.kind == "resource_instruction" and ("memory" if key == "memory_mib" else "walltime") in e.snippet.lower())]
            if not refs:
                continue
            numbers = [_numeric_evidence(key, e) for e in refs]
            if key == "memory_mib" and any(e.kind in {"sbatch.mem-per-cpu", "sbatch.mem-per-gpu"} for e in items):
                numbers.append(None)
            found[key].extend((number, e) for number, e in zip(numbers, refs))
            if None in numbers or any(number is not None and number <= 0 for number in numbers):
                found[key].append((None, refs[0]))
    result = {}
    for key, observations in found.items():
        quantities = {number for number, _ in observations}
        if len(quantities) == 1 and None not in quantities:
            refs = list(dict.fromkeys(e.id for _, e in observations))
            sources = list(dict.fromkeys(f"{e.source_path}:{e.line_start}" for _, e in observations))
            result[key] = ResourceValueRecommendation(value=quantities.pop(), evidence=ResourceValueEvidence(
                source="；".join(sources), reason="复用与当前命令和布局一致的明确运行配置；不代表实测最低需求或实际运行时长。",
                evidence_refs=refs))
    return result


def recommend_resource_values(values, *, environment=None, software=None, project_evidence=None):
    """Verified program/profile > verified catalog rule > exact project evidence.

    Environment resource_options and software version checks alone cannot
    establish a resource recommendation. Queue pressure is not an input.
    """
    if values.executable is None or values.args is None:
        return {}
    root = project_evidence.project_dir if project_evidence else None
    result = _rules(environment.resource_rules, values, root) if environment else {}
    if (software and software.verification_status == "VERIFIED" and
            software.environment_profile == values.environment_profile):
        for key, value in _rules(software.resource_rules, values, root).items():
            result.setdefault(key, value)
    for key, value in _project(project_evidence, values).items():
        result.setdefault(key, value)
    return {key: value for key, value in result.items() if value is not None}
