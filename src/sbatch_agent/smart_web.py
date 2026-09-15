"""Bounded server-side prepared state and presentation-only form conversion."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
from threading import RLock
from time import monotonic
from datetime import datetime, timezone

from .smart_models import PreparationValues, PreparedJob
from .web_forms import FormError, _array, _integer, _reference, profile_choices, profile_key
from .presentation import verification_label, run_type_label
from .ui_formatting import format_args, format_duration
from .resource_policy import POLICY_FIELDS


class PreparedStateError(ValueError):
    pass


@dataclass
class PreparedEntry:
    prepared: PreparedJob
    owner: str
    expires: float
    size: int
    attempted: bool = False
    lock: RLock = field(default_factory=RLock)


class PreparedStore:
    """One Web worker, 30-minute absolute lifetime. Restart requires re-analysis.

    No cookies/hidden inputs containing trusted JobSpecs; no SQLite draft schema.
    Per-entry locks serialize revisions/Confirm. SubmissionService's UUID/SQLite
    claim supplies persistence-level submission protection independently.
    """
    def __init__(self, *, ttl=1800, max_entries=32, max_bytes=16 * 1024 * 1024, clock=monotonic):
        self.ttl, self.max_entries, self.max_bytes, self.clock = ttl, max_entries, max_bytes, clock
        self.entries = {}
        self.lock = RLock()

    def _size(self, p):
        # A conservative bounded serialized representation, not a persistence format.
        return len(repr(p).encode("utf-8"))

    def add(self, prepared, owner):
        size = self._size(prepared)
        with self.lock:
            for key, entry in list(self.entries.items()):
                if entry.expires <= self.clock() and entry.lock.acquire(blocking=False):
                    try:
                        del self.entries[key]
                    finally:
                        entry.lock.release()
            if (size > 4 * 1024 * 1024 or len(self.entries) >= self.max_entries
                or sum(e.size for e in self.entries.values()) + size > self.max_bytes):
                raise PreparedStateError("准备结果已满或项目过大，请缩小项目后重试；仍可手动配置。")
            self.entries[prepared.id] = PreparedEntry(prepared, owner, self.clock() + self.ttl, size)

    @contextmanager
    def use(self, key, owner):
        with self.lock:
            entry = self.entries.get(key)
            if entry is None or entry.owner != owner or entry.expires <= self.clock():
                raise PreparedStateError("准备结果已失效，请重新分析并准备。")
        with entry.lock:
            if entry.expires <= self.clock():
                raise PreparedStateError("准备结果已过期，请重新分析。")
            yield entry

    def replace(self, entry, prepared):
        size = self._size(prepared)
        with self.lock:
            if size > 4 * 1024 * 1024 or sum(e.size for e in self.entries.values()) - entry.size + size > self.max_bytes:
                raise PreparedStateError("准备结果过大，请缩小项目后重新分析。")
            entry.prepared, entry.size = prepared, size


LABELS = {
    "software_id": "运行软件",
    "name": "任务名称", "work_dir": "工作目录", "run_type": "运行方式", "entrypoint": "入口程序",
    "executable": "执行程序", "args": "程序参数", "required_inputs": "输入文件 · JSON 数组",
    "environment_profile": "运行环境", "prepare_steps": "准备步骤 · JSON 数组",
    "launcher_profile": "启动配置", "partition": "分区", "account": "计费账户", "qos": "服务等级（QOS）",
    "nodes": "节点数", "ntasks": "进程数", "cpus_per_task": "每进程 CPU 数", "gpu_count": "每节点 GPU 数 · 0 表示不申请",
    "gpu_type": "GPU 型号", "memory_mib": "每节点内存 · MiB", "time_limit_seconds": "运行时限 · HH:MM:SS",
    "stdout": "标准输出路径", "stderr": "错误输出路径",
}
NUMBERS = {"nodes", "ntasks", "cpus_per_task", "gpu_count", "memory_mib", "time_limit_seconds"}
ARRAYS = {"args", "required_inputs", "prepare_steps"}


def form_values(prepared):
    data = prepared.values.model_dump(mode="json")
    return {key: (format_duration(value) if key == 'time_limit_seconds'
                  else format_args(value) if key == 'args'
                  else profile_key(getattr(prepared.values, key)) if key.endswith("_profile") and value is not None
                  else json.dumps(value, ensure_ascii=False) if key in ARRAYS and value is not None
                  else "" if value is None else str(value)) for key, value in data.items()}


def user_patch(posted, prepared, profiles):
    if set(posted) - PreparationValues.model_fields.keys():
        raise FormError("准备表单含不支持的字段。")
    current = form_values(prepared)
    unresolved = {q.field for q in prepared.unresolved_fields}
    changes = {}
    for key, value in posted.items():
        if key in POLICY_FIELDS:
            mode_key = POLICY_FIELDS[key][0]
            if posted.get(mode_key, getattr(prepared.values, mode_key)) in {"cluster_default", "recommended"}:
                continue  # Inactive inputs are never trusted recommendation values.
        # Untouched advanced controls do not falsely become USER provenance.
        if value == current[key] and (key not in unresolved or not value):
            continue
        if not value:
            changes[key] = None
        elif key in NUMBERS:
            changes[key] = _integer(posted, key)
        elif key in ARRAYS:
            changes[key] = _array(posted, key)
        elif key in {"environment_profile", "launcher_profile"}:
            changes[key] = _reference(value, profiles.environments if key == "environment_profile" else profiles.launchers, LABELS[key])
        else:
            changes[key] = value
    for key, (mode_key, _) in POLICY_FIELDS.items():
        if mode_key in changes and changes[mode_key] == "explicit":
            # Switching from recommendation to explicit deliberately adopts the
            # entered value, even when numerically equal to the last proposal.
            changes[key] = _integer(posted, key) if posted.get(key) else None
    return PreparationValues.model_validate(changes)


def software_choices(entries):
    return [(s.id, f"{s.display_name} / {s.version or '版本未知'} · {verification_label(s.verification_status)}") for s in entries]


def catalog_environment_choices(catalog, profiles):
    registered = {profile_key(e.environment_profile): e for e in catalog.environments if e.environment_profile} if catalog else {}
    choices = [(value, f"{registered[value].display_name} / {registered[value].version or '版本未知'} · {verification_label(registered[value].verification_status)}" if value in registered else label)
               for value, label in profile_choices(profiles.environments)]
    return sorted(choices, key=lambda pair: (pair[0] not in registered, pair[0]))


def page_context(prepared, profiles, attempted=False, posted=None, catalog=None):
    required = {q.field for q in prepared.unresolved_fields}
    options = {"run_type": [(v, run_type_label(v)) for v in ("python", "compiled", "installed")],
               "environment_profile": catalog_environment_choices(catalog, profiles),
               "launcher_profile": profile_choices(profiles.launchers)}
    if catalog:
        options["software_id"] = software_choices(catalog.software)
    for q in prepared.unresolved_fields:
        if q.field == "software_id" and q.choices:
            options[q.field] = software_choices(q.choices)
        elif q.choices and q.field != "environment_profile":
            options[q.field] = profile_choices(q.choices)
    return {"prepared": prepared, "attempted": attempted, "labels": LABELS,
            "review_values": form_values(prepared),
            "snapshot_stale": prepared.snapshot is not None and (datetime.now(timezone.utc) - prepared.snapshot.captured_at).total_seconds() > 300,
            "values": {**form_values(prepared), **(posted or {})}, "options": options,
            "numbers": NUMBERS - {'time_limit_seconds'}, "arrays": ARRAYS - {'args'},
            "advanced_fields": [key for key in LABELS if key not in required and key not in POLICY_FIELDS and (catalog or key != "software_id")],
            "policy_recommendations": prepared.resource_value_recommendations,
            "evidence_by_id": {e.id: e for e in prepared.project_evidence.evidence_items},
            "spec_json": prepared.job_spec.model_dump_json(indent=2) if prepared.job_spec else None}
