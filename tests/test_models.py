"""Model contracts only: no scientific programs or Slurm commands are run."""

import builtins
from copy import deepcopy
from pathlib import Path
import subprocess

import pytest
import yaml
from pydantic import ValidationError

from sbatch_agent import (
    CommandStep,
    EnvironmentProfile,
    GPUResources,
    JobSpec,
    Resources,
    RunStep,
    ShellStep,
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def load_example(name):
    return yaml.safe_load((EXAMPLES / f"{name}.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def data():
    return load_example("python")


@pytest.mark.parametrize("run_type", ["python", "compiled", "installed"])
def test_yaml_examples_and_serialization(run_type):
    spec = JobSpec.model_validate(load_example(run_type))

    assert spec.run_type == run_type
    assert isinstance(spec.environment_profile, EnvironmentProfile)
    assert isinstance(spec.run_step, RunStep)
    assert isinstance(spec.resources, Resources)
    assert all(isinstance(step, CommandStep) for step in spec.prepare_steps)
    assert spec.run_step.launcher_profile is None
    assert JobSpec.model_validate(spec.model_dump()) == spec
    assert JobSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize(
    "field",
    [
        "project_dir",
        "work_dir",
        "run_type",
        "entrypoint",
        "environment_profile",
        "run_step",
        "resources",
        "spec_version",
    ],
)
def test_core_fields_are_required(data, field):
    del data[field]
    with pytest.raises(ValidationError) as caught:
        JobSpec.model_validate(data)
    assert any(error["loc"] == (field,) for error in caught.value.errors())


@pytest.mark.parametrize("run_type", ["bash", "PYTHON", "", None, 1])
def test_unknown_run_type_is_rejected(data, run_type):
    data["run_type"] = run_type
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("project_dir",), "relative/project"),
        (("work_dir",), "~/project"),
        (("work_dir",), "C:\\project"),
        (("work_dir",), "/project\n#SBATCH --nodes=2"),
        (("entrypoint",), "   "),
        (("entrypoint",), "run.py\x00"),
        (("environment_profile", "id"), ""),
        (("environment_profile", "version"), "  "),
        (("environment_profile", "version"), 1),
        (("run_step", "executable"), ""),
        (("run_step", "executable"), "python\rwhoami"),
        (("run_step", "args"), "run.py --input file.json"),
        (("run_step", "args"), ["run.py", 2]),
        (("run_step", "args"), ["run.py", "bad\x00argument"]),
        (("resources", "partition"), "cpu\n#SBATCH --nodes=2"),
        (("resources", "partition"), "cpu\n"),
        (("resources", "partition"), "cpu partition"),
        (("resources", "partition"), "--nodes=2"),
        (("resources", "account"), "bad\raccount"),
        (("resources", "qos"), "normal\x00"),
        (("spec_version",), 0),
        (("spec_version",), True),
        (("spec_version",), "1"),
    ],
)
def test_invalid_values_report_their_field(data, path, value):
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(ValidationError) as caught:
        JobSpec.model_validate(data)
    assert any(error["loc"][: len(path)] == path for error in caught.value.errors())


@pytest.mark.parametrize(
    "field", ["nodes", "ntasks", "cpus_per_task", "memory_mib", "time_limit_seconds"]
)
@pytest.mark.parametrize("value", [0, -1, True, "2", 1.5])
def test_resource_values_must_be_positive_integers(data, field, value):
    data["resources"][field] = value
    with pytest.raises(ValidationError) as caught:
        JobSpec.model_validate(data)
    assert any(
        error["loc"] == ("resources", field) for error in caught.value.errors()
    )


@pytest.mark.parametrize("field", ["partition", "memory_mib", "time_limit_seconds"])
def test_resource_requirements_have_no_invented_defaults(data, field):
    del data["resources"][field]
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


@pytest.mark.parametrize(
    "path",
    [(), ("resources",), ("run_step",), ("environment_profile",)],
)
def test_unknown_fields_are_not_silently_discarded(data, path):
    target = data
    for key in path:
        target = target[key]
    target["misspelled"] = "value"

    with pytest.raises(ValidationError) as caught:
        JobSpec.model_validate(data)
    assert any(
        error["type"] == "extra_forbidden"
        and error["loc"] == (*path, "misspelled")
        for error in caught.value.errors()
    )


def test_literal_argument_boundaries_and_python_module_entry(data):
    args = ["-m", "package.main", "", " two words ", "$(touch marker)", "a;b", "a\nb"]
    data["work_dir"] = "/shared/project with spaces"
    data["entrypoint"] = "package.main"
    data["run_step"] = {"executable": "/opt/python env/bin/python", "args": args}

    spec = JobSpec.model_validate(data)
    assert spec.run_step.args == args
    assert spec.work_dir == "/shared/project with spaces"
    assert JobSpec.model_validate_json(spec.model_dump_json()).run_step.args == args


def test_cpu_defaults_and_typed_gpu_request(data):
    for field in ("nodes", "ntasks", "cpus_per_task"):
        del data["resources"][field]
    data["resources"].update(
        account="research-group", qos="normal", gpus={"count": 1, "gpu_type": "rtx_3090"}
    )
    spec = JobSpec.model_validate(data)
    assert (spec.resources.nodes, spec.resources.ntasks, spec.resources.cpus_per_task) == (
        1, 1, 1
    )
    assert isinstance(spec.resources.gpus, GPUResources)
    assert spec.resources.gpus.gpu_type == "rtx_3090"


@pytest.mark.parametrize("count", [0, -1, True, "1", 1.5])
def test_gpu_count_is_positive_and_strict(data, count):
    data["resources"]["gpus"] = {"count": count}
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


def test_gpu_type_and_unknown_gpu_fields_are_checked(data):
    for gpus in (
        {"count": 1, "gpu_type": "rtx_3090\n#SBATCH --exclusive"},
        {"count": 1, "type_typo": "a100"},
    ):
        data["resources"]["gpus"] = gpus
        with pytest.raises(ValidationError):
            JobSpec.model_validate(data)


def test_compiled_requires_a_preparation_step():
    data = load_example("compiled")
    data["prepare_steps"] = []
    with pytest.raises(ValidationError, match="compiled jobs require"):
        JobSpec.model_validate(data)


def test_preparation_steps_distinguish_commands_from_explicit_shell(data):
    script = 'set -e\ncmake -S . -B "build dir"\ncmake --build "build dir"'
    data["run_type"] = "compiled"
    data["prepare_steps"] = [
        {"kind": "command", "executable": "cmake", "args": ["--version"]},
        {"kind": "shell", "script": script, "work_dir": "/shared/build project"},
    ]
    spec = JobSpec.model_validate(data)
    assert isinstance(spec.prepare_steps[0], CommandStep)
    assert spec.prepare_steps[0].work_dir is None
    assert isinstance(spec.prepare_steps[1], ShellStep)
    assert spec.prepare_steps[1].script == script


@pytest.mark.parametrize(
    "step",
    [
        {"executable": "cmake"},  # The YAML union requires an explicit kind.
        {"kind": "other", "executable": "cmake"},
        {"kind": "command", "executable": "cmake", "script": "echo extra"},
        {"kind": "shell", "script": "  "},
        {"kind": "shell", "script": "echo\x00bad"},
        {"kind": "shell", "script": "echo ok", "args": []},
        {"kind": "command", "executable": "make", "work_dir": "relative"},
    ],
)
def test_invalid_preparation_steps(data, step):
    data["prepare_steps"] = [step]
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


@pytest.mark.parametrize("field", ["nodes", "ntasks"])
def test_parallel_layout_requires_a_profile_reference(data, field):
    data["resources"][field] = 2
    with pytest.raises(ValidationError, match="launcher_profile"):
        JobSpec.model_validate(data)

    # This checks the reference structure, not its existence or verification.
    data["run_step"]["launcher_profile"] = {"id": "example-mpi", "version": "1"}
    assert JobSpec.model_validate(data).run_step.launcher_profile.id == "example-mpi"


def test_evidence_unresolved_and_fingerprints_are_preserved(data):
    data["evidence"] = [
        {
            "field": "entrypoint",
            "source_file": "README.md",
            "line": 12,
            "kind": "direct",
            "detail": "The README gives the entry point.\npython run.py",
        }
    ]
    data["source_fingerprints"] = [{"path": "run.py", "sha256": "a1" * 32}]
    spec = JobSpec.model_validate(data)
    assert spec.evidence[0].line == 12
    assert spec.source_fingerprints[0].sha256 == "a1" * 32
    assert spec.unresolved[0].field == "environment_profile"
    assert JobSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n"])
def test_invalid_sha256_is_rejected(data, digest):
    data["source_fingerprints"] = [{"path": "run.py", "sha256": digest}]
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


@pytest.mark.parametrize(
    ("field", "record"),
    [
        ("evidence", {"field": "entrypoint", "source_file": "README", "line": 0,
                      "kind": "direct", "detail": "entry point"}),
        ("evidence", {"field": "entrypoint", "source_file": "README",
                      "kind": "certain", "detail": "entry point"}),
        ("unresolved", {"field": "resources", "reason": "  "}),
        ("source_fingerprints", {"path": "", "sha256": "a" * 64}),
    ],
)
def test_invalid_metadata_records(data, field, record):
    data[field] = [record]
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


def test_unresolved_does_not_bypass_structural_validation(data):
    data["unresolved"] = [{"field": "resources", "reason": "CPU count unknown"}]
    data["resources"]["cpus_per_task"] = 0
    with pytest.raises(ValidationError):
        JobSpec.model_validate(data)


def test_default_lists_are_not_shared_between_jobs(data):
    for field in ("prepare_steps", "evidence", "unresolved", "source_fingerprints"):
        del data[field]
    data["run_step"].pop("args")
    first = JobSpec.model_validate(data)
    second = JobSpec.model_validate(data)
    for field in ("prepare_steps", "evidence", "unresolved", "source_fingerprints"):
        assert getattr(first, field) is not getattr(second, field)
    first.run_step.args.append("run.py")
    assert second.run_step.args == []


def test_validation_does_not_touch_files_or_launch_processes(data, monkeypatch):
    data["project_dir"] = "/remote/only/nonexistent/project"
    data["prepare_steps"] = [{"kind": "shell", "script": "exit 99"}]
    before = deepcopy(data)

    def unexpected_io(*args, **kwargs):
        pytest.fail("model validation attempted filesystem or subprocess I/O")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", unexpected_io)
        patch.setattr(Path, "open", unexpected_io)
        patch.setattr(Path, "stat", unexpected_io)
        patch.setattr(subprocess, "Popen", unexpected_io)
        spec = JobSpec.model_validate(data)

    assert spec.project_dir == "/remote/only/nonexistent/project"
    assert data == before
