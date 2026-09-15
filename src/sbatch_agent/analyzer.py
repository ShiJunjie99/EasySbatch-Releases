"""Structured inference + deterministic validation, with no execution dependencies."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import PurePosixPath
import re
import shlex

from pydantic import ValidationError

from .analysis_context import AnalysisContextBuilder, AnalysisInputError, safe_relative
from .analysis_models import (
    AIAnalysisResult, AnalysisConflict, DraftFields, FieldProposal, JobSpecDraft,
    ModelMetadata, StructuredAnalysis, field_proposals,
)
from .environment_resolver import EnvironmentResolver
from .model_client import AnalysisOutputValidationError, ModelClient, ModelResponse, ModelUnavailableError
from .model_reliability import RetryingModelClient, RetryPolicy
from .request_trace import traced
from .models import UnresolvedField
from .profiles import StaticProfiles
from .scanner_models import ProjectEvidence


# These are execution/installation/launcher tools, not application entrypoints.
DISALLOWED_EXECUTABLES = frozenset({
    "sh", "bash", "zsh", "fish", "csh", "dash", "eval", "exec", "source", "env",
    "sudo", "su", "rm", "mv", "cp", "curl", "wget", "ssh", "scp", "pip", "pip3",
    "conda", "sbatch", "scancel", "scontrol", "srun", "mpirun", "mpiexec",
})
RESOURCE_DIRECTIVES = {"nodes": "nodes", "ntasks": "ntasks", "cpus_per_task": "cpus-per-task",
                       "gpu_count": "gres", "memory_mib": "mem", "time_limit_seconds": "time"}


def structured_output_schema() -> dict:
    """Provider JSON schema: all keys required, nullable unknowns, no extra keys.

    Local Pydantic validation additionally enforces bounded lengths/counts and
    cross-field checks. No model-specific SDK or free-text schema extraction.
    """
    schema = deepcopy(StructuredAnalysis.model_json_schema())
    def visit(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # Some providers support only the common structural JSON subset.
            for key in ("minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "minItems", "maxItems", "pattern"):
                node.pop(key, None)
            if node.get("type") == "object":
                node["required"] = list(node.get("properties", {}))
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


def _schema_error(exc: ValidationError) -> AnalysisOutputValidationError:
    """Only trusted field names and error categories; no Pydantic input/msg/ctx."""
    error = exc.errors(include_input=False, include_context=False, include_url=False)[0]
    loc = error["loc"]
    field = "output"
    if loc and loc[0] == "draft":
        field = "draft"
        for name, _ in field_proposals(DraftFields()):
            parts = tuple(name.split("."))
            if loc[1:1 + len(parts)] == parts:
                field = name
                break
    elif loc and loc[0] in {"notes", "conflicts"}:
        field = loc[0]
    return AnalysisOutputValidationError(
        "AI output does not match the structured analysis schema.", stage="schema", field=field,
        reason="inconsistent_proposal" if error["type"] == "value_error" else "schema_mismatch",
    )


def _tokens(item):
    try:
        return shlex.split(item.value or item.snippet, comments=True)
    except ValueError:
        return []


def _numeric_evidence(field, item):
    """Only explicit supported quantities; never infer scaling or default values."""
    raw = item.value or ""
    if item.kind == "sbatch." + RESOURCE_DIRECTIVES[field]:
        if field in {"nodes", "ntasks", "cpus_per_task"} and re.fullmatch(r"[0-9]+", raw):
            return int(raw)
        if field == "gpu_count":
            match = re.fullmatch(r"gpu(?::[\w.-]+)?:([1-9][0-9]*)", raw)
            return int(match[1]) if match else None
        if field == "memory_mib":
            match = re.fullmatch(r"([1-9][0-9]*)([MG]?)", raw, re.I)
            return int(match[1]) * (1024 if match[2].upper() == "G" else 1) if match else None
        if field == "time_limit_seconds":
            # Slurm minutes, minutes:seconds, hours:minutes:seconds, days-hours:minutes:seconds.
            if re.fullmatch(r"[0-9]+", raw):
                return int(raw) * 60
            match = re.fullmatch(r"(?:(\d+)-)?(\d+):([0-5]\d):([0-5]\d)", raw)
            if match:
                d, h, m, s = (int(v or 0) for v in match.groups())
                return d * 86400 + h * 3600 + m * 60 + s
            match = re.fullmatch(r"(\d+):([0-5]\d)", raw)
            return int(match[1]) * 60 + int(match[2]) if match else None
    # --gpus is per job, unlike our per-node resource. Only accept --gres here.
    # Small documented sentence grammar; other languages/units remain unknown.
    labels = {"nodes": r"nodes?", "ntasks": r"tasks?", "cpus_per_task": r"CPUs? per task",
              "gpu_count": r"(?:CUDA )?GPUs? per node", "memory_mib": r"MiB (?:of )?memory per node",
              "time_limit_seconds": r"seconds? (?:of )?walltime"}
    match = re.search(r"\brequires\s+([1-9][0-9]*)\s+" + labels[field] + r"\b", item.snippet, re.I)
    return int(match[1]) if match else None


def _unresolved(proposal, reason):
    return {**proposal.model_dump(), "value": None, "status": "UNRESOLVED", "reason": reason}


class AIProjectAnalyzer:
    def __init__(self, *, model_client: ModelClient, profiles: StaticProfiles | None = None,
                 environment_resolver: EnvironmentResolver | None = None,
                 context_builder: AnalysisContextBuilder | None = None,
                 retry_policy: RetryPolicy | None = None, retry_sleeper=None):
        self.model_client = model_client
        self.model_transport = RetryingModelClient(model_client, policy=retry_policy,
                                                  **({"sleeper": retry_sleeper} if retry_sleeper is not None else {}))
        self.profiles = StaticProfiles.model_validate((profiles or StaticProfiles()).model_dump())
        self.environment_resolver = environment_resolver or EnvironmentResolver()
        self.context_builder = context_builder or AnalysisContextBuilder()

    @traced("analyzer")
    def analyze(self, *, evidence: ProjectEvidence, task_intent: str) -> AIAnalysisResult:
        try:
            evidence = ProjectEvidence.model_validate(evidence.model_dump(mode="python"))
        except (AttributeError, ValidationError):
            raise AnalysisInputError("Expected valid ProjectEvidence from ProjectScanner.") from None
        root = PurePosixPath(evidence.project_dir)
        if not root.is_absolute() or ".." in root.parts or str(root) == "/":
            raise AnalysisInputError("Scanner project root is invalid.")
        context = self.context_builder.build(evidence, task_intent)
        try:
            response = self.model_transport.generate_structured(context=context, schema=structured_output_schema())
        except (ModelUnavailableError, AnalysisOutputValidationError):
            raise
        except Exception:
            # Custom clients may embed response text/credentials in exceptions.
            raise ModelUnavailableError("AI model unavailable; manual mode remains available.") from None
        if not isinstance(response, ModelResponse):
            raise AnalysisOutputValidationError("ModelClient must return a structured ModelResponse.")
        try:
            payload = json.dumps(response.data, ensure_ascii=False, allow_nan=False)
            if len(payload.encode("utf-8")) > 128 * 1024:
                raise ValueError
            output = StructuredAnalysis.model_validate(response.data)
        except ValidationError as exc:
            raise _schema_error(exc) from None
        except (ValueError, TypeError, RecursionError):
            raise AnalysisOutputValidationError("AI output does not match the structured analysis schema.", stage="schema") from None
        return self._validate(output, evidence, context, task_intent, response.request_id)

    @traced("post_validation")
    def _validate(self, output, evidence, context, task_intent, request_id):
        index = {e.id: e for e in evidence.evidence_items}
        selected = set(context.evidence_refs)
        known_files = {f.path for f in evidence.files if f.status in {"read", "metadata_only"} and safe_relative(f.path)}
        directories = {"."} | {str(p) for f in known_files for p in PurePosixPath(f).parents if str(p) != "."}
        warnings = list(context.warnings)
        warnings.append("AI draft is unverified; user confirmation is required. No Shell or job was generated.")
        data = output.draft.model_dump()

        def reject(field, message):
            # No raw model values or snippets in exception/log messages.
            reason = ("unknown_evidence" if "evidence reference" in message else
                      "unobserved_path" if "not observed" in message or "not safely observed" in message else
                      "unsupported_value")
            raise AnalysisOutputValidationError(f"{field}: {message}", stage="evidence", field=field, reason=reason)

        def set_proposal(field, proposal):
            parts = field.split(".")
            target = data
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = proposal

        def observed_path(field, path, refs):
            if not safe_relative(path) or str(PurePosixPath(path)) not in known_files:
                reject(field, "path is outside the safely scanned project or was not observed")
            normalized = str(PurePosixPath(path))
            if not any(e.source_path == normalized or normalized in (e.value or "") or normalized in e.snippet for e in refs):
                reject(field, "references do not support this path")

        for field, proposal in field_proposals(output.draft):
            if not set(proposal.evidence_refs) <= selected:
                reject(field, "unknown or excluded evidence reference")
            if proposal.status == "UNRESOLVED":
                continue
            refs = [index[eid] for eid in proposal.evidence_refs]
            value = proposal.value
            if proposal.status == "DIRECT" and not any(e.level.value == "DIRECT" for e in refs):
                set_proposal(field, {**proposal.model_dump(), "status": "INFERRED"})
                warnings.append(f"{field}: cited observations are inferred; DIRECT was downgraded.")
            if field == "run_type":
                if not any(c.value == value and set(c.evidence_ids).intersection(proposal.evidence_refs) for c in evidence.project_type_candidates):
                    reject(field, "run type has no matching scanner candidate")
            elif field == "work_dir":
                if not safe_relative(value, root_allowed=True) or str(PurePosixPath(value)) not in directories:
                    reject(field, "work directory was not observed within the project")
            elif field == "entrypoint":
                if value.kind == "file":
                    observed_path(field, value.value, refs)
                elif value.kind == "module":
                    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", value.value):
                        reject(field, "invalid module name")
                    module_path = value.value.replace(".", "/")
                    if not {module_path + ".py", module_path + "/__main__.py", "src/" + module_path + ".py", "src/" + module_path + "/__main__.py"}.intersection(known_files):
                        reject(field, "module has no scanned source; installation is not inferred")
                    if not any(value.value in (e.value or "") or value.value in e.snippet for e in refs):
                        reject(field, "module reference is unsupported")
                else:
                    self._executable(field, value.value, refs, known_files, reject)
            elif field == "run_step.executable":
                self._executable(field, value, refs, known_files, reject)
            elif field == "run_step.args":
                if len(value) > 64:
                    reject(field, "too many arguments")
                tokens = {t for e in refs for t in _tokens(e)}
                for arg in value:
                    if not arg.isprintable() and arg != "":
                        reject(field, "argument contains controls")
                    if arg in {";", "&&", "||", "|", ">", ">>", "<", "&", "-c"}:
                        reject(field, "Shell operators or inline code flags are not draft arguments")
                    candidate = arg.split("=", 1)[-1]
                    if candidate.startswith(("/", "~")) or ".." in PurePosixPath(candidate).parts:
                        reject(field, "argument contains a project-external path")
                    if "/" in candidate or PurePosixPath(candidate).suffix in {".py", ".json", ".yaml", ".yml", ".toml", ".dat", ".txt", ".in", ".gro", ".top", ".mdp"}:
                        observed_path(field, candidate, refs)
                    if arg not in tokens and not any(arg == e.value for e in refs) and arg not in task_intent:
                        reject(field, "an argument has no supporting evidence or task-intent value")
                    if arg not in tokens and not any(arg == e.value for e in refs):
                        if arg.startswith("-"):
                            reject(field, "task intent cannot invent an unsupported CLI option")
                        set_proposal(field, {**proposal.model_dump(), "status": "INFERRED"})
                        warnings.append("run_step.args: new literal value from task intent; user confirmation required.")
            elif field == "required_inputs":
                if len(value) > 40:
                    reject(field, "too many input paths")
                for path in value:
                    observed_path(field, path, refs)
            elif field == "environment_requirements":
                names = [*value.dependencies, *value.software]
                if value.python_min_version:
                    names.append(value.python_min_version)
                if not all(any(name.lower() in ((e.value or "") + "\n" + e.snippet).lower() for e in refs) for name in names):
                    reject(field, "environment requirements are not supported by cited text")
                if any(re.search(r"\b" + re.escape(name) + r"\s*(?:[<>=!~;]|\[)", e.snippet, re.I)
                       for name in value.dependencies for e in refs):
                    set_proposal(field, _unresolved(proposal, "依赖含版本/extra/marker 约束；当前 Resolver 仅匹配包名，不能静默忽略约束。"))
                    warnings.append("environment_requirements: unsupported dependency constraints remain unresolved.")
                if value.python_min_version:
                    version = re.escape(value.python_min_version)
                    minimum = re.compile(r"\bpython(?:\s+version)?\s*(?:>=\s*" + version + r"|" + version + r"\s*\+)(?![\d.])", re.I)
                    if not any(minimum.search(e.snippet) for e in refs) or any(
                        re.search(r"\b(?:requires[-_]python|python(?:\s+version)?)\b[^\n]*?(?:<=?|==|!=|~=)", e.snippet, re.I)
                        for e in refs
                    ):
                        set_proposal(field, _unresolved(proposal, "Python 版本不是可确认的单一最低版本要求；精确版本或范围约束需后续解析。"))
                        warnings.append("environment_requirements: Python version constraints remain unresolved.")
            elif field == "build":
                if not any(value.system in ((e.value or "") + e.snippet).lower() for e in refs):
                    reject(field, "build system is not supported by cited text")
                if value.target and not any(c.kind == "cmake_target" and c.value == value.target and set(c.evidence_ids).intersection(proposal.evidence_refs) for c in evidence.executable_candidates):
                    set_proposal(field, _unresolved(proposal, "Build target 未经 Scanner 确认；构建命令需后续 Resolver。"))
            elif field.startswith("parallelism."):
                kind = field.split(".")[1]
                # Positive hints only establish possible use, never a requirement.
                if value and kind != "serial" and any(e.kind == kind for e in refs):
                    set_proposal(field, {**proposal.model_dump(), "status": "INFERRED"})
                    warnings.append(f"{field}: hint of possible use, not a mandatory resource requirement.")
                else:
                    set_proposal(field, _unresolved(proposal, "串行/不使用某并行方式不能由线索缺失证明，需用户确认。"))
            elif field.startswith("resource_requirements."):
                name = field.split(".")[1]
                quantities = {_numeric_evidence(name, e) for e in refs} - {None}
                if proposal.status != "DIRECT" or quantities != {value}:
                    set_proposal(field, _unresolved(proposal, "没有唯一且明确的资源数量依据；不接受性能猜测。"))
                    warnings.append(f"{field}: unsupported/conflicting quantity converted to UNRESOLVED.")
                elif any(e.kind.startswith("sbatch.") for e in refs):
                    warnings.append(f"{field}: historical sbatch request, not a verified minimum or current cluster permission.")

        conflicts = list(output.conflicts)
        valid_fields = {field for field, _ in field_proposals(output.draft)}
        for conflict in conflicts:
            if conflict.field not in valid_fields or not set(conflict.evidence_refs) <= selected:
                reject("conflicts", "invalid field or evidence reference")
            original = dict(field_proposals(output.draft))[conflict.field]
            set_proposal(conflict.field, _unresolved(original, conflict.reason))

        # Preserve competing entrypoint observations even when the model omits
        # a conflict. Explicit intent naming a candidate is the narrow safe
        # deterministic disambiguation; broader semantic choices remain inferred.
        commands = [c for c in evidence.entrypoint_candidates if c.priority <= 35 and set(c.evidence_ids) & selected]
        targets = sorted({c.value for c in commands})
        if len(targets) > 1 and not any(target in task_intent for target in targets):
            warnings.append("Multiple explicit entrypoints exist; semantic selection requires human confirmation.")
            if output.draft.entrypoint.value is not None:
                if not any(c.field == "entrypoint" for c in conflicts):
                    refs = sorted({eid for c in commands for eid in c.evidence_ids if eid in selected})[:16]
                    if len(refs) >= 2:
                        conflicts.append(AnalysisConflict(field="entrypoint", reason="README/脚本包含不同入口，需确认任务意图与入口。", evidence_refs=refs))
                set_proposal("entrypoint", _unresolved(output.draft.entrypoint, "多个显式入口仍需确认；未静默选择。"))

        fields = DraftFields.model_validate(data)
        entry = fields.entrypoint.value
        if entry and fields.run_type.value == "python" and fields.run_step.args.value is not None:
            args = fields.run_step.args.value
            if entry.kind in {"file", "module"} and entry.value not in args and "./" + entry.value not in args:
                reject("run_step.args", "Python entrypoint and invocation disagree")
        resolution = self.environment_resolver.resolve(fields.environment_requirements.value, self.profiles)
        unresolved = [UnresolvedField(field=field, reason=p.reason) for field, p in field_proposals(fields) if p.status == "UNRESOLVED"]
        unresolved.extend([
            UnresolvedField(field="resources.partition", reason="由用户/Profile/现有推荐器另行确定；AI 不选 partition。"),
            UnresolvedField(field="confirmation", reason="草稿必须由用户确认，M6-B 不转换为正式 JobSpec。"),
        ])
        if resolution.status != "MATCHED":
            unresolved.append(UnresolvedField(field="environment_profile", reason=resolution.reason))
        if fields.run_type.value == "compiled":
            unresolved.append(UnresolvedField(field="prepare_steps", reason="构建信息尚非可执行步骤；需后续确定性 Resolver/用户确认。"))
        if fields.parallelism.mpi.value or fields.parallelism.threads.value:
            unresolved.append(UnresolvedField(field="launcher_profile", reason="并行线索未匹配正式启动布局，不能自动选择 launcher。"))
        try:
            metadata = ModelMetadata(provider=self.model_client.provider, model=self.model_client.model,
                                     analyzed_at=datetime.now(timezone.utc), request_id=request_id)
        except (AttributeError, ValidationError):
            reject("model_metadata", "invalid client metadata")
        return AIAnalysisResult(
            draft=JobSpecDraft(**fields.model_dump(), project_dir=evidence.project_dir,
                               environment_resolution=resolution, unresolved=unresolved),
            conflicts=conflicts, warnings=sorted(set(warnings)), notes=output.notes,
            model_metadata=metadata, context_evidence_refs=list(context.evidence_refs),
        )

    @staticmethod
    def _executable(field, value, refs, known_files, reject):
        if (not safe_relative(value) or re.search(r"[\s;|&<>$`(){}'\"\\]", value)
                or value.startswith("-") or PurePosixPath(value).name in DISALLOWED_EXECUTABLES):
            reject(field, "executable must be a literal project program, not Shell/tools/external path")
        if "/" in value and str(PurePosixPath(value)) not in known_files:
            reject(field, "executable file was not safely observed")
        if not any(value in _tokens(e) or value == e.value or str(PurePosixPath(value)) == e.source_path for e in refs):
            reject(field, "executable is not supported by cited evidence")
