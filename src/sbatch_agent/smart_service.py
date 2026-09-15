"""Single-user preparation orchestration. No CLI, SQL, model tools or auto-submit."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .analysis_models import field_proposals
from .environment_resolver import EnvironmentResolver
from .model_client import ModelErrorCode, ModelUnavailableError
from .request_trace import current_trace, phase, traced
from .models import Evidence, JobSpec, Resources, SourceFingerprint
from .persistence import PersistenceError, RecordNotFoundError, SubmissionState
from .profiles import StaticProfiles
from .project_checks import check_project, project_path
from .recommendation_models import RecommendationRequest, RequestedResources
from .recommender import ResourceRecommender
from .renderer import render_job_script
from .scanner import ProjectScanner
from .smart_models import FieldOrigin, FieldSource, PreparationValues, PreparedJob, UnresolvedQuestion
from .server_catalog import ServerCatalog, provenance, compatibility
from .software_resolver import SoftwareResolver, SoftwareResolution
from .resource_policy import POLICY_FIELDS, UNAVAILABLE, policy_data, recommend_resource_values


class PreparationError(ValueError):
    """Safe application error; preparation can continue after user correction."""


RESOURCE_FIELDS = ("partition", "account", "qos", "nodes", "ntasks", "cpus_per_task",
                   "gpu_count", "gpu_type", "memory_mib", "time_limit_seconds")
REQUIRED_FIELDS = ("name", "work_dir", "run_type", "entrypoint", "executable", "args",
                   "environment_profile", "required_inputs", "prepare_steps", "partition",
                   "nodes", "ntasks", "cpus_per_task", "gpu_count", "memory_mib", "time_limit_seconds")
STALE_SECONDS = 300


def _resource_values(resources):
    data = resources.model_dump()
    data.pop("memory_policy", None)
    data.pop("walltime_policy", None)
    gpu = data.pop("gpus")
    return {**data, "gpu_count": gpu["count"] if gpu else 0, "gpu_type": gpu["gpu_type"] if gpu else None}


def _resources(values, recommendations=None):
    data = {k: getattr(values, k) for k in RESOURCE_FIELDS if k not in {"gpu_count", "gpu_type"}}
    data["gpus"] = {"count": values.gpu_count, "gpu_type": values.gpu_type} if values.gpu_count else None
    data.update(policy_data(values, recommendations or {}))
    return data


def _lookup(ref, profiles):
    return next((p for p in profiles if ref and (p.id, p.version) == (ref.id, ref.version)), None)


class SmartJobService:
    def __init__(self, *, analyzer, cluster_service, profiles: StaticProfiles,
                 scanner=None, recommender=None, environment_resolver=None, clock=None,
                 catalog: ServerCatalog | None = None):
        self.scanner = scanner if scanner is not None else ProjectScanner()
        self.analyzer = analyzer
        self.cluster = cluster_service
        self.profiles = StaticProfiles.model_validate(profiles.model_dump())
        self.recommender = recommender if recommender is not None else ResourceRecommender()
        self.catalog = (ServerCatalog.model_validate(catalog.model_dump()).validate_profiles(self.profiles)
                        if catalog is not None else None)
        self.environment_resolver = environment_resolver or EnvironmentResolver(self.catalog)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @traced("prepare")
    def prepare(self, *, project_dir, task_intent, user_values: PreparationValues | None = None):
        if self.analyzer is None:
            raise ModelUnavailableError(code=ModelErrorCode.NOT_CONFIGURED)
        with phase("scanner"):
            evidence = self.scanner.scan(project_dir)
        current_trace().scan_completed = True
        analysis = self.analyzer.analyze(evidence=evidence, task_intent=task_intent)
        with phase("environment"):
            resolution = self.environment_resolver.resolve(analysis.draft.environment_requirements.value, self.profiles)
        prepared = PreparedJob(str(uuid4()), self.clock(), evidence, analysis, resolution)
        prepared.user_values = PreparationValues.model_validate(
            (user_values or PreparationValues()).model_dump(exclude_unset=True))
        prepared.prepare_request_id = current_trace().request_id
        try:
            with phase("cluster", failure_code="CLUSTER_UNAVAILABLE"):
                prepared.snapshot = self.cluster.get_snapshot()
        except Exception:
            prepared.warnings.append("集群快照暂不可用；保留 AI 分析，请手工确认 partition/resources。")
        return self._complete(prepared)

    def finalize(self, *, prepared: PreparedJob, user_values: PreparationValues | None = None,
                 recommendation_id: str | None = None):
        """Use server-held evidence; no additional model or cluster requests."""
        result = deepcopy(prepared)
        check_project(result.project_evidence, self.scanner.config)
        update = PreparationValues.model_validate((user_values or PreparationValues()).model_dump(exclude_unset=True))
        changes = update.model_dump(exclude_unset=True)
        if recommendation_id is not None:
            choice = next((r for r in (prepared.resource_recommendations.recommendations if prepared.resource_recommendations else ())
                           if r.id == recommendation_id), None)
            if choice is None:
                raise PreparationError("推荐已失效，请重新检查资源方案。")
            changes = {**_resource_values(choice.proposed_resources), **changes}
        for key, (mode_key, _) in POLICY_FIELDS.items():
            if key in update.model_fields_set and mode_key not in update.model_fields_set:
                # An edited number is always the user's value, not a recommendation.
                changes[mode_key] = "explicit"
            if changes.get(mode_key) in {"cluster_default", "recommended"}:
                changes[key] = None
        result.user_values = PreparationValues.model_validate({**result.user_values.model_dump(exclude_unset=True), **changes})
        result.revision += 1
        return self._complete(result)

    @traced("catalog")
    def _software(self, p):
        if self.catalog is None:
            return None
        selected = p.user_values.software_id
        if selected:
            entry = self.catalog.software_by_id(selected)
            if entry is None:
                raise PreparationError("Software catalog ID 未登记。")
            return SoftwareResolution(status="MATCHED", choices=[entry], reason="用户明确选择 Catalog 软件。")
        draft = p.ai_analysis.draft
        run_type = p.user_values.run_type or draft.run_type.value
        if run_type in {"python", "compiled"}:
            return None  # Dependencies are not the Python interpreter/build target.
        names = []
        if draft.run_step.executable.value:
            names.append(draft.run_step.executable.value)
        if draft.entrypoint.value and draft.entrypoint.value.kind == "command":
            names.append(draft.entrypoint.value.value)
        req = draft.environment_requirements.value
        names.extend(req.software if req else [])
        resolutions = [SoftwareResolver().resolve(n, self.catalog) for n in dict.fromkeys(names)]
        if (resolutions and draft.run_step.executable.value
                and draft.run_step.executable.status == "DIRECT" and not resolutions[0].choices):
            return resolutions[0]  # A dependency must not replace a direct invocation.
        known = [r for r in resolutions if r.choices]
        if known:
            # The actual invocation wins over software dependencies. A user's
            # explicit selection still wins in the branch above.
            return known[0]
        return resolutions[0] if resolutions else SoftwareResolver().resolve(None, self.catalog)

    def _selected_software(self, p):
        resolution = p.software_resolution
        return resolution.choices[0] if resolution and resolution.status == "MATCHED" else None

    def _base(self, p):
        root = p.project_evidence.project_dir
        data, origins = {}, {}

        def put(key, value, source, reason, refs=()):
            data[key] = value
            origins[key] = FieldOrigin(source, reason, tuple(refs))

        defaults = {"name": Path(root).name, "work_dir": root, "nodes": 1, "ntasks": 1,
                    "cpus_per_task": 1, "gpu_count": 0, "account": None, "qos": None,
                    "launcher_profile": None}
        for key, value in defaults.items():
            reason = ("使用 Scanner 已验证的项目目录及其末级名称。" if key in {"name", "work_dir"}
                      else "沿用正式模型的单节点、单任务、单 CPU、无 GPU / 可选字段省略规则；不推断性能。")
            put(key, value, FieldSource.SYSTEM_DEFAULT, reason)
        for key, proposal in field_proposals(p.ai_analysis.draft):
            target = key.removeprefix("run_step.").removeprefix("resource_requirements.")
            if target not in PreparationValues.model_fields or proposal.value is None:
                continue
            value = proposal.value
            if target == "entrypoint":
                value = value.value
            if target == "work_dir":
                value = str(project_path(root, value))
            put(target, value, FieldSource.AI_DIRECT if proposal.status == "DIRECT" else FieldSource.AI_INFERRED,
                proposal.reason, proposal.evidence_refs)
        # Analyzer paths are relative to project root, even if working directory
        # is a subdirectory. Resolve only actually observed file argument tokens.
        observed = {f.path for f in p.project_evidence.files if f.status in {"read", "metadata_only"}}
        if "args" in data:
            def argument(arg):
                prefix, sep, value = arg.partition("=")
                path = value if sep and prefix.startswith("-") else arg
                key = path.removeprefix("./")
                if key in observed:
                    absolute = str(project_path(root, path))
                    return prefix + "=" + absolute if sep and prefix.startswith("-") else absolute
                return arg
            data["args"] = [argument(a) for a in data["args"]]
        if "required_inputs" in data:
            data["required_inputs"] = [str(project_path(root, v)) for v in data["required_inputs"]]
        elif not p.project_evidence.input_candidates:
            put("required_inputs", [], FieldSource.SYSTEM_DEFAULT, "未发现必要输入候选；沿用空声明，不证明项目没有文件依赖。")
        if data.get("run_type") != "compiled":
            put("prepare_steps", [], FieldSource.SYSTEM_DEFAULT, "非 compiled 模式不自动添加构建操作。")
        for signal, fields in (("mpi", ("ntasks",)), ("threads", ("cpus_per_task",)), ("gpu", ("gpu_count",))):
            hints = getattr(p.ai_analysis.draft.parallelism, signal).value
            if hints:
                for key in fields:
                    if origins.get(key) and origins[key].source == FieldSource.SYSTEM_DEFAULT:
                        data.pop(key, None)
                        origins.pop(key, None)
        if p.environment_resolution.status == "MATCHED":
            put("environment_profile", p.environment_resolution.choices[0],
                FieldSource.SERVER_CATALOG if self.catalog else FieldSource.ENVIRONMENT_RESOLVER,
                p.environment_resolution.reason)
        software = self._selected_software(p)
        if software:
            put("software_id", software.id, FieldSource.SERVER_CATALOG, provenance(software))
            if software.verification_status in {"VERIFIED", "DOCUMENTED"}:
                names = {software.id.casefold(), software.display_name.casefold(), *(a.casefold() for a in software.aliases)}
                existing = data.get("executable")
                if (p.user_values.software_id or existing is None or existing.casefold() in names
                        or origins["executable"].source == FieldSource.AI_INFERRED):
                    put("executable", software.executable, FieldSource.SERVER_CATALOG, provenance(software))
                if data.get("run_type") is None or origins["run_type"].source == FieldSource.AI_INFERRED:
                    put("run_type", software.run_type, FieldSource.SERVER_CATALOG, provenance(software))
                if not data.get("entrypoint"):
                    put("entrypoint", software.id, FieldSource.SERVER_CATALOG, provenance(software))
                if software.launch_profile:
                    put("launcher_profile", software.launch_profile, FieldSource.SERVER_CATALOG, provenance(software))
        user = p.user_values.model_dump(exclude_unset=True)
        # First resolve explicit environment so its declared shape can supply
        # only unambiguous defaults, never choosing an arbitrary registered shape.
        ref = p.user_values.environment_profile if "environment_profile" in user else data.get("environment_profile")
        env = _lookup(ref, self.profiles.environments)
        catalog_env = self.catalog.environment(ref) if self.catalog else None
        if catalog_env:
            if "environment_profile" in origins:
                origins["environment_profile"] = FieldOrigin(FieldSource.SERVER_CATALOG, provenance(catalog_env))
            # Resolve the interpreter of a Python invocation, never replace a
            # project executable or an explicit user command with another tool.
            import re
            if (data.get("run_type") == "python" and catalog_env.python_executable
                    and catalog_env.verification_status in {"VERIFIED", "DOCUMENTED"}
                    and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", data.get("executable") or "")):
                put("executable", catalog_env.python_executable, FieldSource.SERVER_CATALOG, provenance(catalog_env))
        if env and env.resource_options:
            shapes = [_resource_values(option.shape) for option in env.resource_options]
            for key in ("nodes", "ntasks", "cpus_per_task", "gpu_count", "gpu_type", "memory_mib"):
                values = [s[key] for s in shapes]
                if all(v == values[0] for v in values) and (key not in data or origins[key].source == FieldSource.SYSTEM_DEFAULT):
                    put(key, values[0], FieldSource.PROFILE_DEFAULT, "所有登记 resource_options 的此字段一致。")
        for key, value in user.items():
            put(key, value, FieldSource.USER, "用户最后确认的值；优先于自动建议。")
        work_dir = data.get("work_dir") or root
        for key, suffix in (("stdout", "out"), ("stderr", "err")):
            if key not in user:
                put(key, str(Path(work_dir) / f"sbatch-{p.id}-%j.{suffix}"), FieldSource.SYSTEM_DEFAULT,
                    "工作目录下的内部 UUID + Slurm %j 日志名；不创建目录。")
        return PreparationValues.model_validate(data), origins

    @traced("finalization")
    def _complete(self, p):
        p.job_spec, p.rendered_script = None, None
        if self.catalog:
            p.software_resolution = self._software(p)
            p.environment_resolution = self.environment_resolver.resolve(
                p.ai_analysis.draft.environment_requirements.value, self.profiles,
                software=self._selected_software(p),
                python_required=(p.user_values.run_type or p.ai_analysis.draft.run_type.value) == "python")
            # A post-validator downgrade must not be undone by generic catalog
            # matching after it deliberately removed an unsupported constraint.
            proposal = p.ai_analysis.draft.environment_requirements
            if proposal.status == "UNRESOLVED" and proposal.evidence_refs:
                from .analysis_models import EnvironmentResolution
                p.environment_resolution = EnvironmentResolution(status="UNRESOLVED", reason=proposal.reason)
        p.values, p.resolved_fields = self._base(p)
        p.resource_recommendations, p.selected_recommendation = None, None
        p.unresolved_fields = []
        # Rebuild transient warnings, retaining only acquisition failure above.
        p.warnings = [w for w in p.warnings if w.startswith("集群快照暂不可用")]
        p.warnings.extend(p.project_evidence.warnings)
        p.warnings.extend(p.ai_analysis.warnings)
        p.warnings.append("默认布局不是性能或无 GPU 需求的证明；提交前请核对程序、参数和资源。")
        v = p.values
        def question(key, reason, choices=()):
            if not any(q.field == key for q in p.unresolved_fields):
                p.unresolved_fields.append(UnresolvedQuestion(key, reason, tuple(choices)))
        software = self._selected_software(p)
        if p.software_resolution and p.software_resolution.status != "MATCHED":
            if not p.user_values.executable:
                question("software_id", p.software_resolution.reason,
                         p.software_resolution.choices or self.catalog.software)
        if software:
            p.warnings.append(provenance(software))
            if software.verification_status in {"INFERRED", "UNVERIFIED"} and not p.user_values.executable:
                question("executable", "软件未有可靠验证，不自动采用其执行路径；需用户确认。")
            if software.verification_status in {"VERIFIED", "DOCUMENTED"} and software.compatible_partitions is not None and v.partition and v.partition not in software.compatible_partitions:
                question("partition", "该 partition 不在软件 Catalog 的已知兼容范围。")
        catalog_env = self.catalog.environment(v.environment_profile) if self.catalog else None
        if catalog_env:
            p.warnings.append(provenance(catalog_env))
            if catalog_env.verification_status in {"VERIFIED", "DOCUMENTED"} and catalog_env.available_partitions is not None and v.partition and v.partition not in catalog_env.available_partitions:
                question("partition", "该 partition 不在环境 Catalog 的已知适用范围。")
        env = _lookup(v.environment_profile, self.profiles.environments)
        if v.environment_profile and env is None:
            raise PreparationError("Environment profile 未登记。")
        if env and "environment_profile" in p.user_values.model_fields_set:
            choices = p.environment_resolution.choices
            if choices and not _lookup(v.environment_profile, choices):
                p.warnings.append("用户显式选择了自动匹配候选之外的登记环境；未验证它满足全部分析需求，请核对。")
            elif not choices:
                p.warnings.append("环境由用户显式选择；登记能力不足以验证全部 AI 环境需求。")
        if not env:
            question("environment_profile", p.environment_resolution.reason,
                     p.environment_resolution.choices or self.profiles.environments)
        if env and env.allowed_partitions is not None and v.partition and v.partition not in env.allowed_partitions:
            question("partition", "该 partition 不在 EnvironmentProfile 的允许列表。")
        launch = _lookup(v.launcher_profile, self.profiles.launchers)
        if v.launcher_profile and launch is None:
            raise PreparationError("Launch profile 未登记。")
        if ((v.nodes or 1) > 1 or (v.ntasks or 1) > 1 or p.ai_analysis.draft.parallelism.mpi.value) and not launch:
            question("launcher_profile", "并行布局必须选择已登记启动器；不自动添加 srun。", self.profiles.launchers)
        if launch and launch.supported_layouts is not None and not any(
            l.nodes == v.nodes and l.ntasks == v.ntasks for l in launch.supported_layouts
        ):
            question("launcher_profile", "启动器不支持当前 nodes/ntasks 布局。", self.profiles.launchers)
        if v.run_type == "compiled" and not v.prepare_steps:
            question("prepare_steps", "请确认结构化构建步骤；AI build hint 不会自动成为执行命令。")
        if v.gpu_type and not v.gpu_count:
            question("gpu_count", "指定 GPU 型号时数量必须大于 0。")
        if p.snapshot and (self.clock() - p.snapshot.captured_at).total_seconds() > STALE_SECONDS:
            p.warnings.append("Cluster snapshot 已超过 300 秒；资源建议可能过期，本次不会静默刷新或更改用户选择。")
        p.resource_value_recommendations = recommend_resource_values(
            v, environment=env, software=software, project_evidence=p.project_evidence)
        values = v.model_dump()
        for key, (mode_key, _) in POLICY_FIELDS.items():
            mode = getattr(v, mode_key)
            if mode == "cluster_default":
                values[key] = None
                p.resolved_fields[key] = FieldOrigin(FieldSource.CLUSTER_DEFAULT,
                    "用户选择使用集群默认；不生成对应资源声明，最终由 Slurm / QOS / 账户策略决定。")
            elif mode == "recommended":
                candidate = p.resource_value_recommendations.get(key)
                values[key] = candidate.value if candidate else None
                if candidate:
                    p.resolved_fields[key] = FieldOrigin(FieldSource.RESOURCE_POLICY_RECOMMENDATION,
                        candidate.evidence.reason, tuple(candidate.evidence.evidence_refs))
                else:
                    p.resolved_fields.pop(key, None)
                    question(key, UNAVAILABLE[key])
            elif mode == "explicit":
                values[key] = getattr(p.user_values, key)
                if values[key] is None:
                    p.resolved_fields.pop(key, None)
                    question(key, "请填写手动指定的值。")
                else:
                    p.resolved_fields[key] = FieldOrigin(FieldSource.USER, "用户最后指定的值；推荐器不会替换。")
        p.values = v = PreparationValues.model_validate(values)
        can_recommend = env and v.run_type and all(getattr(v, key) is not None for key in
                        ("nodes", "ntasks", "cpus_per_task", "gpu_count")) and all(
                            getattr(v, key) is not None or getattr(v, pair[0]) == "cluster_default"
                            for key, pair in POLICY_FIELDS.items())
        if p.snapshot and can_recommend and not p.unresolved_fields:
            request = RecommendationRequest(run_type=v.run_type, environment_profile=v.environment_profile,
                launcher_profile=v.launcher_profile, resources=RequestedResources.model_validate(
                    _resources(v, p.resource_value_recommendations)))
            if self.catalog:
                request.catalog_compatibility = compatibility(software, catalog_env)
            try:
                with phase("recommendation", failure_code="RECOMMENDATION_UNAVAILABLE"):
                    report = self.recommender.recommend(spec=request, snapshot=p.snapshot, profiles=self.profiles,
                        as_of=self.clock(), fixed_memory=v.memory_mode is not None)
            except Exception:
                p.warnings.append("资源推荐暂不可用；用户可手工确认资源，基础校验仍然执行。")
            else:
                p.resource_recommendations = report
                # User fields are exact final choices, not minima to silently grow.
                fixed = p.user_values.model_dump(exclude_unset=True)
                candidates = [r for r in report.recommendations if all(
                    _resource_values(r.proposed_resources)[k] == value
                    for k, value in fixed.items() if k in RESOURCE_FIELDS and value is not None)]
                if any(getattr(v, pair[0]) == "recommended" for pair in POLICY_FIELDS.values()):
                    compatible = []
                    for candidate in candidates:
                        final_values = PreparationValues.model_validate({**v.model_dump(), **_resource_values(candidate.proposed_resources)})
                        recommendations = recommend_resource_values(final_values, environment=env, software=software,
                            project_evidence=p.project_evidence)
                        if all(recommendations.get(key) == p.resource_value_recommendations.get(key)
                               for key, pair in POLICY_FIELDS.items() if getattr(v, pair[0]) == "recommended"):
                            compatible.append(candidate)
                    candidates = compatible
                if candidates:
                    p.selected_recommendation = candidates[0]
                    resources = _resource_values(candidates[0].proposed_resources)
                    p.values = PreparationValues.model_validate({**v.model_dump(), **resources})
                    for key in RESOURCE_FIELDS:
                        if key not in fixed and (key not in p.resolved_fields or getattr(v, key) != resources[key]):
                            p.resolved_fields[key] = FieldOrigin(FieldSource.RESOURCE_RECOMMENDER,
                                "采用当前可行候选排名第一项；依据和快照时间见资源建议。")
                else:
                    reason = "；".join(reason for r in report.rejections for reason in r.reasons)
                    question("partition", "没有满足明确资源要求的兼容方案。" + reason)
        v = p.values
        for key in REQUIRED_FIELDS:
            if key in POLICY_FIELDS and getattr(v, POLICY_FIELDS[key][0]) == "cluster_default":
                continue
            if getattr(v, key) is None:
                if key == "partition" and p.snapshot and not can_recommend:
                    # Ask for prerequisites first; the recommender will choose
                    # partition once memory/time/environment are supplied.
                    continue
                question(key, "没有可靠值，请用户确认。" if key not in {"memory_mib", "time_limit_seconds"}
                         else "缺少有依据的内存 / 时限需求，不猜默认值。")
        # Conflicts remain visible and require explicit input in the affected
        # editable field, rather than being erased by a mechanical default.
        for conflict in p.conflicts:
            key = conflict.field.removeprefix("run_step.").removeprefix("resource_requirements.")
            if key in POLICY_FIELDS and getattr(v, POLICY_FIELDS[key][0]) in {"cluster_default", "explicit"}:
                continue
            if key in PreparationValues.model_fields and key not in p.user_values.model_fields_set:
                question(key, "证据冲突需确认：" + conflict.reason)
        if p.unresolved_fields:
            return p
        if env.resource_options and not any(v.partition in option.partitions and all(
            getattr(v, k) == value for k, value in _resource_values(option.shape).items()
            if k != "memory_mib" or v.memory_mode not in {"cluster_default", "recommended", "explicit"})
            for option in env.resource_options):
            question("partition", "最终资源组合不属于此 EnvironmentProfile 的登记 resource_options；请修改资源或环境。")
            return p
        p.job_spec = self._spec(p)
        self._check_paths(p)
        p.rendered_script = render_job_script(p.job_spec, profiles=self.profiles)
        return p

    def _spec(self, p):
        v = p.values
        evidence = {e.id: e for e in p.project_evidence.evidence_items}
        provenance = [Evidence(field=key, source_file=evidence[ref].source_path,
            line=evidence[ref].line_start, kind="direct" if origin.source in {FieldSource.AI_DIRECT, FieldSource.RESOURCE_POLICY_RECOMMENDATION} else "inferred",
            detail=origin.reason) for key, origin in p.resolved_fields.items() for ref in origin.evidence_refs if ref in evidence]
        provenance.extend(Evidence(field=key, source_file="server_catalog.yaml", kind="inferred",
                                   detail=origin.reason)
                          for key, origin in p.resolved_fields.items() if origin.source == FieldSource.SERVER_CATALOG)
        return JobSpec(project_dir=p.project_evidence.project_dir, work_dir=v.work_dir, run_type=v.run_type,
            entrypoint=v.entrypoint, environment_profile=v.environment_profile, prepare_steps=v.prepare_steps,
            run_step={"executable": v.executable, "args": v.args, "launcher_profile": v.launcher_profile},
            resources=Resources.model_validate(_resources(v, p.resource_value_recommendations)), job_name=v.name, stdout=v.stdout, stderr=v.stderr,
            required_inputs=v.required_inputs, spec_version=p.revision, evidence=provenance,
            source_fingerprints=[SourceFingerprint(path=f.path, sha256=f.sha256) for f in p.project_evidence.source_fingerprints])

    def _check_paths(self, p):
        v, root = p.values, p.project_evidence.project_dir
        files = [str(project_path(root, str(Path(v.work_dir) / f))) for f in v.required_inputs]
        entry = p.ai_analysis.draft.entrypoint.value
        # Compiled output may not exist until build. Module/installed command
        # names are not filesystem paths. A user-selected Python file is checked.
        if v.run_type == "python" and ((entry and entry.kind == "file") or v.entrypoint.endswith(".py")):
            files.append(str(project_path(root, v.entrypoint)))
        directories = [v.work_dir]
        for log in (v.stdout, v.stderr):
            if log is not None:
                directories.append(str(project_path(root, str(Path(v.work_dir) / log)).parent))
        check_project(p.project_evidence, self.scanner.config, directories=directories, files=files)

    def confirm(self, *, prepared: PreparedJob, submission_service):
        """Explicit user action only. One reserved UUID across duplicate requests."""
        if prepared.state != "READY_TO_SUBMIT":
            raise PreparationError("任务仍有 unresolved，不能提交。")
        # A fresh duplicate must never allocate another internal record. Even
        # concurrent callers converge on this UUID and the existing SQLite claim.
        repo = submission_service.repository
        try:
            record = repo.get(prepared.id)
        except RecordNotFoundError:
            record = None
        if record is not None and record.submission_state != SubmissionState.SCRIPT_RENDERED:
            if record.job_spec != prepared.job_spec or record.rendered_script != prepared.rendered_script:
                raise PreparationError("此内部 ID 已绑定不同提交快照。")
            return record
        self._check_paths(prepared)
        spec = self._spec(prepared)
        script = render_job_script(spec, profiles=self.profiles)
        if spec != prepared.job_spec or script != prepared.rendered_script:
            raise PreparationError("准备配置发生变化，请重新 Final Review。")
        # Also ensure the current SubmissionService registry produces the exact
        # reviewed script, before any database or external submission effects.
        if render_job_script(spec, profiles=submission_service.profiles) != script:
            raise PreparationError("Profile 配置已变化，请重新分析并检查脚本。")
        if record is None:
            try:
                record = submission_service.create_job(spec=spec, name=prepared.values.name, record_id=prepared.id)
            except PersistenceError:
                # Another caller may have created this same immutable record.
                # If it did not, preserve the original persistence failure.
                try:
                    record = repo.get(prepared.id)
                except RecordNotFoundError:
                    raise
        if record.job_spec != spec or record.rendered_script != script:
            raise PreparationError("此内部 ID 已绑定不同的提交快照，不能覆盖。")
        if record.submission_state != SubmissionState.SCRIPT_RENDERED:
            return record
        return submission_service.submit_job(record.id)
