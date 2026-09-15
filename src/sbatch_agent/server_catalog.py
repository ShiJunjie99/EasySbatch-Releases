"""Maintainer-owned server facts. Loading never probes software or runs commands."""

from datetime import datetime
from pathlib import Path, PurePosixPath
import os
import stat
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, Field, model_validator

from .models import EnvironmentProfile, Identifier, ProfileReference, RunType, SingleLineText, _Model
from .profiles import EnvironmentCapabilities, StaticProfiles, VerifiedResourceRule


VerificationStatus = Literal["VERIFIED", "DOCUMENTED", "INFERRED", "UNVERIFIED"]
Name = Annotated[SingleLineText, Field(max_length=500)]


def _path(value):
    path = PurePosixPath(value)
    if (not path.is_absolute() or str(path) != value or value == "/"
            or ".." in path.parts or re.search(r"[\\$`\n\r\x00]", value)):
        raise ValueError("expected a normalized absolute POSIX path without expansion")
    return value


CatalogPath = Annotated[Name, AfterValidator(_path)]


def _command(value):
    if value.startswith("/"):
        return _path(value)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", value):
        raise ValueError("expected an absolute executable path or a literal command name")
    return value


def _time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


class CatalogSource(_Model):
    document: Name
    section: Name
    detail: Name


class CatalogFact(_Model):
    id: Identifier
    display_name: Name
    version: Name | None = None
    source: CatalogSource
    verification_status: VerificationStatus
    last_verified_at: Annotated[datetime, BeforeValidator(_time)] | None = None
    verification_scope: Name

    @model_validator(mode="after")
    def verification(self):
        if self.last_verified_at and self.last_verified_at.utcoffset() is None:
            raise ValueError("verification timestamp must include a timezone")
        if self.verification_status == "VERIFIED" and self.last_verified_at is None:
            raise ValueError("VERIFIED requires an actual verification timestamp and scope")
        return self


class EnvironmentCatalogEntry(CatalogFact):
    type: Literal["system_python", "conda", "venv", "module", "shell_profile", "other"]
    python_executable: CatalogPath | None = None
    environment_profile: EnvironmentProfile | None = None
    available_partitions: list[Identifier] | None = None
    capabilities: EnvironmentCapabilities | None = None


class SoftwareCatalogEntry(CatalogFact):
    aliases: list[Name] = Field(default_factory=list, max_length=50)
    executable: Annotated[Name, AfterValidator(_command)]
    environment_profile: EnvironmentProfile | None = None
    run_type: RunType = "installed"
    parallelism: list[Literal["serial", "threads", "mpi", "gpu"]] = Field(default_factory=list)
    compatible_partitions: list[Identifier] | None = None
    launch_profile: ProfileReference | None = None
    resource_rules: list[VerifiedResourceRule] = Field(default_factory=list, max_length=32)


class CompilerCatalogEntry(CatalogFact):
    kind: Literal["c", "cxx", "fortran", "make", "cmake", "cuda"]
    executable: Annotated[Name, AfterValidator(_command)]
    environment_profile: EnvironmentProfile | None = None


class CatalogMetadata(_Model):
    schema_version: Literal[1] = 1
    description: Name
    # Identifies an audit baseline, not current cluster topology or availability.
    source_document: Name


def key(reference):
    return reference.id, reference.version


class CatalogError(ValueError):
    """Safe configuration error: never echo a YAML value or parser excerpt."""


class ServerCatalog(_Model):
    metadata: CatalogMetadata
    environments: list[EnvironmentCatalogEntry] = Field(default_factory=list, max_length=200)
    software: list[SoftwareCatalogEntry] = Field(default_factory=list, max_length=200)
    compilers: list[CompilerCatalogEntry] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def identities(self):
        for group in (self.environments, self.software, self.compilers):
            if len({e.id.casefold() for e in group}) != len(group):
                raise ValueError("duplicate catalog id")
        # IDs/aliases are globally unique within the software namespace. A
        # shared display_name deliberately represents ambiguous versions.
        owners = {}
        for software in self.software:
            for name in {software.id.casefold(), *(a.casefold() for a in software.aliases)}:
                if name in owners and owners[name] != software.id:
                    raise ValueError("duplicate software alias")
                owners[name] = software.id
        references = [key(e.environment_profile) for e in self.environments if e.environment_profile]
        if len(set(references)) != len(references):
            raise ValueError("one catalog environment per exact profile reference is required")
        return self

    def validate_profiles(self, profiles: StaticProfiles):
        profiles = StaticProfiles.model_validate(profiles.model_dump())
        envs = {key(e) for e in profiles.environments}
        launches = {key(e) for e in profiles.launchers}
        for entry in [*self.environments, *self.software, *self.compilers]:
            if entry.environment_profile and key(entry.environment_profile) not in envs:
                raise CatalogError("Catalog environment profile reference does not exist.")
        for entry in self.software:
            if entry.launch_profile and key(entry.launch_profile) not in launches:
                raise CatalogError("Catalog launch profile reference does not exist.")
        catalog_envs = {key(e.environment_profile) for e in self.environments if e.environment_profile}
        if any(e.environment_profile and key(e.environment_profile) not in catalog_envs for e in self.software):
            raise CatalogError("Software environment must also have a catalog environment entry.")
        return self

    @classmethod
    def load(cls, path: str | Path, *, profiles: StaticProfiles):
        import yaml
        class UniqueLoader(yaml.SafeLoader):
            pass
        def mapping(loader, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                name = loader.construct_object(key_node, deep=deep)
                if name in result:
                    raise CatalogError("Duplicate YAML mapping key in catalog.")
                result[name] = loader.construct_object(value_node, deep=deep)
            return result
        UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
        try:
            with Path(path).open("rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise CatalogError("Catalog must be a regular file.")
                raw = stream.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                raise CatalogError("Catalog exceeds configuration size limit.")
            return cls.model_validate(yaml.load(raw, Loader=UniqueLoader)).validate_profiles(profiles)
        except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
            raise CatalogError("Invalid server catalog; check schema, identities, paths and profile references.") from None

    def environment(self, reference):
        return next((e for e in self.environments if reference and e.environment_profile
                     and key(e.environment_profile) == key(reference)), None)

    def software_by_id(self, identifier):
        return next((s for s in self.software if s.id == identifier), None)


def provenance(entry):
    checked = entry.last_verified_at.isoformat() if entry.last_verified_at else "not checked"
    return f"Server catalog · {entry.verification_status} · Last checked: {checked} · {entry.verification_scope}"


def compatibility(software, environment):
    """Unknown applicability is None, not an empty allow-list or inferred GPU need."""
    from .recommendation_models import CatalogCompatibility
    trusted = lambda e: e if e and e.verification_status in {"VERIFIED", "DOCUMENTED"} else None
    software, environment = trusted(software), trusted(environment)
    scopes = [value for value in (software.compatible_partitions if software else None,
              environment.available_partitions if environment else None) if value is not None]
    return CatalogCompatibility(software_id=software.id if software else None,
        allowed_partitions=sorted(set.intersection(*(set(s) for s in scopes))) if scopes else None,
        capabilities=software.parallelism if software else [])
