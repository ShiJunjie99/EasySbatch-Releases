"""Public EasySbatch API with lazy imports.

The server package exposes the same names as before while allowing the frozen
client Launcher to import its small platform-neutral modules without pulling
Pydantic, SQLite and Slurm implementation code into the client artifact.
"""

from importlib import import_module


_EXPORTS = {
    "Command": (".models", "Command"),
    "CommandStep": (".models", "CommandStep"),
    "EnvironmentProfile": (".models", "EnvironmentProfile"),
    "Evidence": (".models", "Evidence"),
    "GPUResources": (".models", "GPUResources"),
    "JobSpec": (".models", "JobSpec"),
    "PrepareStep": (".models", "PrepareStep"),
    "ProfileReference": (".models", "ProfileReference"),
    "Resources": (".models", "Resources"),
    "ResourceMode": (".models", "ResourceMode"),
    "ResourceValuePolicy": (".models", "ResourceValuePolicy"),
    "ResourceValueEvidence": (".models", "ResourceValueEvidence"),
    "ResourceValueRecommendation": (".models", "ResourceValueRecommendation"),
    "RunStep": (".models", "RunStep"),
    "RunType": (".models", "RunType"),
    "ShellStep": (".models", "ShellStep"),
    "SourceFingerprint": (".models", "SourceFingerprint"),
    "UnresolvedField": (".models", "UnresolvedField"),
    "EnvironmentDefinition": (".profiles", "EnvironmentDefinition"),
    "LaunchDefinition": (".profiles", "LaunchDefinition"),
    "StaticProfiles": (".profiles", "StaticProfiles"),
    "VerifiedResourceRule": (".profiles", "VerifiedResourceRule"),
    "JobSpecValidationError": (".renderer", "JobSpecValidationError"),
    "render_job_script": (".renderer", "render_job_script"),
    "CommandResult": (".runner", "CommandResult"),
    "CommandRunner": (".runner", "CommandRunner"),
    "SlurmCommandError": (".runner", "SlurmCommandError"),
    "SubprocessRunner": (".runner", "SubprocessRunner"),
    "JobState": (".slurm", "JobState"),
    "JobStatus": (".slurm", "JobStatus"),
    "SlurmClient": (".slurm", "SlurmClient"),
    "SlurmParseError": (".slurm", "SlurmParseError"),
    "SubmissionResult": (".slurm", "SubmissionResult"),
    "resolve_log_path": (".slurm", "resolve_log_path"),
    "DB_SCHEMA_VERSION": (".persistence", "DB_SCHEMA_VERSION"),
    "JobRecord": (".persistence", "JobRecord"),
    "JobRepository": (".persistence", "JobRepository"),
    "PersistenceError": (".persistence", "PersistenceError"),
    "RecordNotFoundError": (".persistence", "RecordNotFoundError"),
    "SchemaVersionError": (".persistence", "SchemaVersionError"),
    "SubmissionConflictError": (".persistence", "SubmissionConflictError"),
    "SubmissionState": (".persistence", "SubmissionState"),
    "JobNotSubmittableError": (".service", "JobNotSubmittableError"),
    "JobNotSubmittedError": (".service", "JobNotSubmittedError"),
    "SubmissionService": (".service", "SubmissionService"),
    "SubmissionServiceError": (".service", "SubmissionServiceError"),
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted((*globals(), *_EXPORTS))
