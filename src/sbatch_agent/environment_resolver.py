"""Exact declared capability matching; no discovery, activation, or installation."""

import re

from .analysis_models import EnvironmentRequirements, EnvironmentResolution
from .models import EnvironmentProfile
from .profiles import StaticProfiles
from .server_catalog import ServerCatalog, key
from .software_resolver import SoftwareResolver


def _package(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _version(value):
    return tuple(int(p) for p in value.split(".")) + (0,) * (3 - len(value.split(".")))


class EnvironmentResolver:
    def __init__(self, catalog: ServerCatalog | None = None):
        self.catalog = catalog

    def resolve(self, requirements: EnvironmentRequirements | None, profiles: StaticProfiles,
                *, software=None, python_required=False) -> EnvironmentResolution:
        profiles = StaticProfiles.model_validate(profiles.model_dump())
        if self.catalog is not None:
            return self._catalog_resolve(requirements, profiles, software, python_required)
        if requirements is None or not any((requirements.python_min_version, requirements.dependencies, requirements.software)):
            return EnvironmentResolution(status="UNRESOLVED", reason="环境需求不足，不能从 profile 名称猜测匹配。")
        requirements = EnvironmentRequirements.model_validate(requirements.model_dump())
        matches = []
        for profile in sorted(profiles.environments, key=lambda p: (p.id, p.version)):
            cap = profile.analysis_capabilities
            if cap is None:
                continue
            if requirements.python_min_version and (cap.python_version is None or _version(cap.python_version) < _version(requirements.python_min_version)):
                continue
            if not {_package(p) for p in requirements.dependencies} <= {_package(p) for p in cap.dependencies}:
                continue
            if not set(requirements.software) <= set(cap.software):
                continue
            matches.append(EnvironmentProfile(id=profile.id, version=profile.version))
        status = "MATCHED" if len(matches) == 1 else "MULTIPLE" if matches else "NO_MATCH"
        return EnvironmentResolution(status=status, choices=matches, reason={
            "MATCHED": "唯一登记能力匹配；仅为建议，实际环境及未列出的版本约束仍需确认。",
            "MULTIPLE": "多个登记环境满足已知需求，请用户选择。",
            "NO_MATCH": "No registered environment matches requirements；不安装或虚构环境。",
        }[status])

    def _catalog_resolve(self, requirements, profiles, software, python_required):
        catalog = self.catalog.validate_profiles(profiles)
        requirements = requirements or EnvironmentRequirements()
        requirements = EnvironmentRequirements.model_validate(requirements.model_dump())
        constrained = software is not None or python_required or any((requirements.python_min_version,
                      requirements.dependencies, requirements.software))
        if not constrained:
            return EnvironmentResolution(status="UNRESOLVED", reason="环境需求不足，不按 Catalog 名称猜测。")
        scopes = []
        for name in requirements.software:
            matches = SoftwareResolver().resolve(name, catalog).choices
            # An explicit version selection narrows a family-name requirement.
            if software and any(s.id == software.id for s in matches):
                matches = [software]
            scopes.append({key(s.environment_profile) for s in matches if s.environment_profile
                           and s.verification_status in {"VERIFIED", "DOCUMENTED"}})
            if not matches:
                scopes[-1] = {key(e.environment_profile) for e in catalog.environments
                              if e.environment_profile and e.capabilities and name.casefold() in
                              {hint.casefold() for hint in e.capabilities.software}}
        if software:
            scopes.append({key(software.environment_profile)} if software.environment_profile
                          and software.verification_status in {"VERIFIED", "DOCUMENTED"} else set())
        matches = []
        for entry in sorted(catalog.environments, key=lambda e: e.id):
            reference, cap = entry.environment_profile, entry.capabilities
            if reference is None or entry.verification_status not in {"VERIFIED", "DOCUMENTED"}:
                continue
            if any(key(reference) not in scope for scope in scopes):
                continue
            if python_required and entry.python_executable is None:
                continue
            if requirements.python_min_version and (not cap or cap.python_version is None
                    or _version(cap.python_version) < _version(requirements.python_min_version)):
                continue
            if requirements.dependencies and (not cap or not {_package(p) for p in requirements.dependencies}
                                             <= {_package(p) for p in cap.dependencies}):
                continue
            matches.append(reference)
        status = "MATCHED" if len(matches) == 1 else "MULTIPLE" if matches else "NO_MATCH"
        return EnvironmentResolution(status=status, choices=matches, reason={
            "MATCHED": "Server catalog 唯一环境匹配；加载方式引用既有 EnvironmentProfile，验证范围需审阅。",
            "MULTIPLE": "Server catalog 有多个满足需求的环境，需用户确认；不按版本号自动选择。",
            "NO_MATCH": "Server catalog 缺少满足已知需求的登记环境；请人工补齐，不安装或猜测。",
        }[status])
