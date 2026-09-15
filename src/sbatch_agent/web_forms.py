"""Translate form strings into the existing JobSpec; no rendering or I/O."""

import json
import re

from pydantic import ValidationError

from .models import JobSpec
from .profiles import StaticProfiles
from .recommendation_models import RecommendationRequest
from .ui_formatting import field_label, parse_args, parse_duration
from .resource_policy import POLICY_FIELDS, UNAVAILABLE, recommend_resource_values
from .models import ResourceValuePolicy


DEFAULT_FORM = {
    "software_id": "",
    "name": "", "project_dir": "", "work_dir": "", "run_type": "python",
    "entrypoint": "", "environment": "", "executable": "", "args": "[]",
    "required_inputs": "[]", "prepare_steps": "[]", "partition": "",
    "account": "", "qos": "", "nodes": "1", "ntasks": "1",
    "cpus_per_task": "1", "gpu_count": "0", "gpu_type": "",
    "memory_mib": "", "time_limit_seconds": "", "stdout": "", "stderr": "",
    "memory_mode": "cluster_default", "walltime_mode": "cluster_default",
    "launcher": "", "spec_version": "1", "evidence": "[]",
    "source_fingerprints": "[]", "unresolved": "[]",
    "preference": "BALANCED",
}


class FormError(ValueError):
    """A human-readable form error, safe for escaped HTML display."""


def apply_catalog_to_form(form, catalog, *, explicit=False):
    """Only a registered ID is accepted; custom values remain explicit choices."""
    result = form.copy()
    if not result.get("software_id"):
        return result
    entry = catalog.software_by_id(result["software_id"]) if catalog else None
    if entry is None:
        raise FormError("所选软件尚未登记，请重新选择。")
    if entry.verification_status not in {"VERIFIED", "DOCUMENTED"}:
        raise FormError("软件尚未确认，请在高级设置中核对执行程序与运行环境。")
    for name, value in (("executable", entry.executable), ("entrypoint", entry.id),
                        ("run_type", entry.run_type)):
        if explicit or not result.get(name):
            result[name] = value
    if not result.get("environment") and entry.environment_profile:
        result["environment"] = profile_key(entry.environment_profile)
    if not result.get("launcher") and entry.launch_profile:
        result["launcher"] = profile_key(entry.launch_profile)
    partition = result.get("partition")
    env = next((e for e in catalog.environments if e.environment_profile
                and profile_key(e.environment_profile) == result.get("environment")), None)
    for allowed in (entry.compatible_partitions, env.available_partitions if env else None):
        if allowed is not None and partition and partition not in allowed:
            raise FormError("所选分区不在该软件或环境的已知兼容范围。")
    return result


def profile_key(profile) -> str:
    return json.dumps([profile.id, profile.version], ensure_ascii=False)


def profile_choices(profiles) -> list[tuple[str, str]]:
    return [(profile_key(p), f"{p.id} / {p.version}") for p in profiles]


def _reference(value: str, profiles, label: str) -> dict:
    for profile in profiles:
        if value == profile_key(profile):
            return {"id": profile.id, "version": profile.version}
    raise FormError(f"{label} 未找到，请从已登记配置中选择。")


def _integer(form: dict[str, str], key: str) -> int:
    value = form.get(key, "")
    if key == "memory_mib" and value.endswith(" MiB"):
        value = value[:-4]
    if key == 'time_limit_seconds':
        try:
            return parse_duration(value)
        except ValueError as exc:
            raise FormError(str(exc)) from exc
    if not re.fullmatch(r"[0-9]{1,12}", value):
        raise FormError(f"{field_label(key)}请填写整数。")
    return int(value)


def _array(form: dict[str, str], key: str) -> list:
    if key == 'args':
        try:
            return parse_args(form.get(key, ''))
        except ValueError as exc:
            raise FormError(str(exc)) from exc
    try:
        value = json.loads(form.get(key, "[]"))
        # JSON permits escaped lone surrogates; SQLite/UTF-8 snapshots do not.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise FormError(f"{field_label(key)}必须是合法 JSON 数组。") from exc
    if not isinstance(value, list):
        raise FormError(f"{field_label(key)}必须是 JSON 数组。")
    return value


def validation_messages(exc: ValidationError) -> list[str]:
    # Do not include Pydantic's input/context dump or traceback.
    messages = []
    hints = {'missing': '请填写此项。', 'greater_than': '请填写大于 0 的数值。',
             'int_type': '请填写整数。', 'string_type': '请填写文本。',
             'string_too_short': '此项不能为空。', 'list_type': '请填写数组。',
             'extra_forbidden': '包含不支持的字段。', 'literal_error': '请选择有效选项。',
             'string_pattern_mismatch': '格式不正确，请检查输入。'}
    for error in exc.errors(include_input=False, include_url=False)[:10]:
        key = '.'.join(map(str, error['loc']))
        hint = hints.get(error['type'], '不符合运行配置要求，请查看校验详情。')
        messages.extend([f'{field_label(key)}：{hint}', f"校验位置：{key or 'JobSpec'}: {error['msg']}"])
    return messages


def resources_from_form(form: dict[str, str], recommendations=None) -> dict:
    """Representation conversion shared by manual creation and advisory preview."""
    resources = {key: _integer(form, key) for key in (
        "nodes", "ntasks", "cpus_per_task",
    )}
    for key, (mode_key, policy_key) in POLICY_FIELDS.items():
        mode = form.get(mode_key, "explicit")
        evidence = None
        if mode == "recommended":
            candidate = (recommendations or {}).get(key)
            if candidate is None:
                raise FormError(UNAVAILABLE[key])
            resources[key], evidence = candidate.value, candidate.evidence
        elif mode == "cluster_default":
            resources[key] = None
        elif mode == "explicit":
            resources[key] = _integer(form, key)
        else:
            raise FormError("请选择有效的资源策略。")
        resources[policy_key] = ResourceValuePolicy(mode=mode, evidence=evidence).model_dump()
    resources.update({"partition": form.get("partition", ""),
                      "account": form.get("account") or None,
                      "qos": form.get("qos") or None})
    count = _integer(form, "gpu_count")
    if count == 0 and form.get("gpu_type"):
        raise FormError("GPU 数量为 0 时不能填写 GPU 型号。")
    resources["gpus"] = ({"count": count, "gpu_type": form.get("gpu_type") or None}
                         if count else None)
    return resources


def recommendation_request_from_form(form: dict[str, str], profiles: StaticProfiles, *, recommendations=None) -> RecommendationRequest:
    if _array(form, "unresolved"):
        raise FormError("请先补齐待确认项，再请求推荐。")
    resources = resources_from_form(form, manual_resource_recommendations(form, profiles) if recommendations is None else recommendations)
    resources["partition"] = resources["partition"] or None
    return RecommendationRequest.model_validate({
        "resources": resources, "run_type": form.get("run_type", ""),
        "environment_profile": _reference(form.get("environment", ""), profiles.environments, "运行环境"),
        "launcher_profile": _reference(form["launcher"], profiles.launchers, "启动配置") if form.get("launcher") else None,
    })


def apply_resources_to_form(form: dict[str, str], resources) -> dict[str, str]:
    result = form.copy()
    for key in ("partition", "nodes", "ntasks", "cpus_per_task", "memory_mib", "time_limit_seconds", "account", "qos"):
        value = getattr(resources, key)
        result[key] = "" if value is None else str(value)
    result["gpu_count"] = str(resources.gpus.count if resources.gpus else 0)
    result["gpu_type"] = (resources.gpus.gpu_type or "") if resources.gpus else ""
    return result


def spec_from_form(form: dict[str, str], profiles: StaticProfiles, *, recommendations=None) -> JobSpec:
    """Parse only representation; existing models/renderer decide validity."""
    if not form.get("name", "").strip():
        raise FormError("任务名称不能为空。")
    resources = resources_from_form(form, manual_resource_recommendations(form, profiles) if recommendations is None else recommendations)
    launcher = (_reference(form["launcher"], profiles.launchers, "启动配置")
                if form.get("launcher") else None)
    return JobSpec.model_validate({
        **{key: form.get(key, "") for key in (
            "project_dir", "work_dir", "run_type", "entrypoint",
        )},
        "job_name": form["name"],
        "environment_profile": _reference(form.get("environment", ""),
                                            profiles.environments, "运行环境"),
        "run_step": {"executable": form.get("executable", ""),
                     "args": _array(form, "args"), "launcher_profile": launcher},
        "resources": resources,
        "spec_version": _integer(form, "spec_version"),
        "stdout": form.get("stdout") or None, "stderr": form.get("stderr") or None,
        **{key: _array(form, key) for key in (
            "prepare_steps", "required_inputs", "evidence", "unresolved", "source_fingerprints",
        )},
    })


def manual_resource_recommendations(form, profiles, catalog=None):
    """Recompute trusted profile/catalog rules from the current form; no model/I/O.

    Unvalidated manual text is never a recommendation or a verification fact.
    Partial/invalid forms display unavailable and still use ordinary validation.
    """
    from .smart_models import PreparationValues
    environment = next((p for p in profiles.environments if profile_key(p) == form.get("environment")), None)
    software = catalog.software_by_id(form.get("software_id")) if catalog and form.get("software_id") else None
    if software and (not software.environment_profile or profile_key(software.environment_profile) != form.get("environment")):
        software = None
    try:
        values = PreparationValues(run_type=form.get("run_type"), executable=form.get("executable") or None,
            environment_profile={"id": environment.id, "version": environment.version} if environment else None,
            args=_array(form, "args"), nodes=_integer(form, "nodes"), ntasks=_integer(form, "ntasks"),
            cpus_per_task=_integer(form, "cpus_per_task"), gpu_count=_integer(form, "gpu_count"),
            gpu_type=form.get("gpu_type") or None)
        return recommend_resource_values(values, environment=environment, software=software)
    except (ValueError, TypeError):
        return {}
