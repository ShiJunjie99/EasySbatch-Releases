"""Offline renderer contracts, including execution of harmless Bash probes."""

import builtins
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest
import yaml

from sbatch_agent import (
    Command,
    EnvironmentDefinition,
    JobSpec,
    JobSpecValidationError,
    LaunchDefinition,
    ProfileReference,
    StaticProfiles,
    render_job_script,
)


ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def profiles():
    return StaticProfiles.model_validate(load_yaml(ROOT / "examples/profiles.yaml"))


@pytest.fixture
def empty_profiles():
    return StaticProfiles(environments=[
        EnvironmentDefinition(id="none", version="1", load_steps=[])
    ])


@pytest.fixture
def spec(tmp_path):
    return JobSpec.model_validate({
        "project_dir": str(tmp_path), "work_dir": str(tmp_path),
        "run_type": "python", "entrypoint": "probe.py",
        "environment_profile": {"id": "none", "version": "1"},
        "run_step": {
            "executable": sys.executable,
            "args": ["-c", "import json, sys; print(json.dumps(sys.argv[1:]))"],
        },
        "resources": {"partition": "test_cpu", "memory_mib": 128,
                      "time_limit_seconds": 60},
        "spec_version": 1,
    })


def run_bash(script, cwd):
    # No submission: Bash ignores #SBATCH comments. Programs here are probes
    # created by the test, not the scientific examples or cluster commands.
    return subprocess.run(
        ["bash", "--noprofile", "--norc"], input=script, text=True,
        capture_output=True, cwd=cwd, timeout=10,
        env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
    )


def semantic_lines(script):
    return [
        line.strip() for line in script.splitlines()
        if line.strip() and (not line.lstrip().startswith("#") or line.startswith("#SBATCH"))
    ]


@pytest.mark.parametrize("name", ["python", "compiled", "installed"])
def test_golden_scripts(name, profiles):
    proposal = JobSpec.model_validate(load_yaml(ROOT / f"examples/rendering/{name}.yaml"))
    expected = (ROOT / f"tests/fixtures/{name}.sbatch").read_text(encoding="utf-8")
    script = render_job_script(proposal, profiles=profiles)
    assert script.startswith("#!/usr/bin/env bash\n")
    assert script.endswith("\n")
    assert semantic_lines(script) == semantic_lines(expected)
    assert render_job_script(proposal, profiles=profiles) == script
    assert "srun" not in script and "mpirun" not in script
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr


@pytest.mark.parametrize("name", ["python", "compiled", "installed"])
def test_original_examples_still_require_resolution(name, profiles):
    proposal = JobSpec.model_validate(load_yaml(ROOT / f"examples/{name}.yaml"))
    with pytest.raises(JobSpecValidationError, match="unresolved.environment_profile"):
        render_job_script(proposal, profiles=profiles)


@pytest.mark.parametrize("argument", [
    "ordinary/path", "case 01.json", "single'quote", 'double"quote',
    "$HOME", "$(touch injected)", "`touch injected`", "case 01;touch injected",
    "left & touch injected", "(parentheses)", "", " leading and trailing ",
    "first\n#SBATCH --nodes=999\nsecond", "back\\slash", "*?[abc]",
])
def test_arguments_reach_the_program_literally(spec, empty_profiles, tmp_path, argument):
    spec.run_step.args.append(argument)
    script = render_job_script(spec, profiles=empty_profiles)
    result = run_bash(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [argument]
    assert not (tmp_path / "injected").exists()
    # Slurm stops reading directives before any command arguments occur.
    assert script.index("set -e") < script.index("# Run")


@pytest.mark.parametrize("name", ["if", "A=B", "tool with 'quotes' and $;"])
def test_executable_names_remain_words(spec, empty_profiles, tmp_path, name):
    executable = tmp_path / name
    executable.write_text("#!/usr/bin/env bash\nprintf '%s' invoked\n")
    executable.chmod(0o755)
    # Use a bare shell keyword/assignment as a PATH command for the first two.
    spec.run_step.executable = name if name in ("if", "A=B") else str(executable)
    spec.run_step.args = []
    empty_profiles.environments[0].load_steps = [Command(
        executable="export", args=[f"PATH={tmp_path}:{os.environ['PATH']}"]
    )]
    result = run_bash(render_job_script(spec, profiles=empty_profiles), tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "invoked"


def test_work_dir_and_inputs_with_special_characters(spec, empty_profiles, tmp_path):
    work = tmp_path / 'space \' " $ ; & (dir) \\ #'
    work.mkdir()
    input_name = "input 'x';touch injected"
    (work / input_name).write_text("data")
    spec.work_dir = str(work)
    spec.required_inputs = [input_name]
    spec.run_step.args = ["-c", "import os; print(os.getcwd())"]
    script = render_job_script(spec, profiles=empty_profiles)
    chdir_line = next(line for line in script.splitlines() if line.startswith("#SBATCH --chdir"))
    # For the encoder's double-quoted/backslash-escaped subset, shlex agrees
    # with Slurm's get_argument; importantly there is exactly ONE option token.
    assert shlex.split(chdir_line[len("#SBATCH "):], comments=True) == [f"--chdir={work}"]
    result = run_bash(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(work)
    assert not (work / "injected").exists()


@pytest.mark.parametrize("field", ["job_name", "work_dir", "stdout", "stderr"])
@pytest.mark.parametrize("control", ["\n#SBATCH --nodes=999", "\r", "\x00", "\t", "\u2028"])
def test_directive_control_injection_is_rejected(spec, empty_profiles, field, control):
    # Mutation deliberately bypasses construction validation, exercising render.
    setattr(spec, field, "/shared/value" + control)
    with pytest.raises(JobSpecValidationError):
        render_job_script(spec, profiles=empty_profiles)


@pytest.mark.parametrize("field", ["job_name", "stdout", "stderr"])
def test_directive_quotes_cannot_add_options(spec, empty_profiles, field):
    value = 'safe" --nodes=999 # \\ $HOME ; & (x)'
    setattr(spec, field, value)
    script = render_job_script(spec, profiles=empty_profiles)
    option = {"job_name": "job-name", "stdout": "output", "stderr": "error"}[field]
    line = next(line for line in script.splitlines() if line.startswith(f"#SBATCH --{option}="))
    assert shlex.split(line[len("#SBATCH "):], comments=True) == [f"--{option}={value}"]
    assert script.count("#SBATCH --nodes=") == 1


@pytest.mark.parametrize("field,value", [
    ("partition", "cpu\n#SBATCH --nodes=9"), ("account", "user\r"),
    ("qos", "normal --exclusive"), ("nodes", 0), ("ntasks", -1),
    ("cpus_per_task", True), ("memory_mib", "128"),
    ("time_limit_seconds", "01:60:00"), ("time_limit_seconds", 0),
])
def test_invalid_resource_values_are_revalidated(spec, empty_profiles, field, value):
    setattr(spec.resources, field, value)
    with pytest.raises(JobSpecValidationError, match=field):
        render_job_script(spec, profiles=empty_profiles)


@pytest.mark.parametrize("seconds,expected", [
    (1, "00:00:01"), (59, "00:00:59"), (60, "00:01:00"),
    (3600, "01:00:00"), (86399, "23:59:59"), (86400, "1-00:00:00"),
    (90061, "1-01:01:01"),
])
def test_walltime_has_canonical_units(spec, empty_profiles, seconds, expected):
    spec.resources.time_limit_seconds = seconds
    script = render_job_script(spec, profiles=empty_profiles)
    assert f"#SBATCH --time={expected}\n" in script


def test_optional_directives_are_omitted(spec, empty_profiles):
    script = render_job_script(spec, profiles=empty_profiles)
    for option in ("account", "qos", "gres", "job-name", "output", "error"):
        assert f"#SBATCH --{option}=" not in script
    assert "#SBATCH --ntasks=1\n" in script
    assert "#SBATCH --cpus-per-task=1\n" in script


@pytest.mark.parametrize("gpu,expected", [
    ({"count": 2}, "gpu:2"),
    ({"count": 1, "gpu_type": "rtx_3090"}, "gpu:rtx_3090:1"),
])
def test_gpu_request_uses_existing_per_node_representation(spec, empty_profiles, gpu, expected):
    data = spec.model_dump()
    data["resources"]["gpus"] = gpu
    script = render_job_script(JobSpec.model_validate(data), profiles=empty_profiles)
    assert f"#SBATCH --gres={expected}\n" in script
    assert "--gpus-per-task" not in script


def test_source_and_shell_functions_persist_before_nounset(spec, empty_profiles, tmp_path):
    setup = tmp_path / "environment setup.sh"
    setup.write_text(
        ': "$UNSET_DURING_SOURCE"\n'
        'module() { export PROFILE_RESULT="$*"; }\n'
    )
    empty_profiles.environments[0].load_steps = [
        Command(executable="source", args=[str(setup)]),
        Command(executable="module", args=["load", "name with spaces"]),
    ]
    spec.run_step.args = ["-c", "import os; print(os.environ['PROFILE_RESULT'])"]
    script = render_job_script(spec, profiles=empty_profiles)
    assert script.index("'source'") < script.index("set -u")
    result = run_bash(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "load name with spaces"


def test_missing_input_stops_before_preparation(spec, empty_profiles, tmp_path):
    data = spec.model_dump()
    data["required_inputs"] = ["missing.json"]
    data["prepare_steps"] = [{"kind": "command", "executable": "touch", "args": ["prepared"]}]
    result = run_bash(render_job_script(JobSpec.model_validate(data), profiles=empty_profiles), tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "prepared").exists()
    assert result.stdout == ""


@pytest.mark.parametrize("step", [
    {"kind": "command", "executable": "false"},
    {"kind": "shell", "script": "false\nprintf should-not-run"},
    {"kind": "shell", "script": "false | true\nprintf should-not-run"},
])
def test_failed_build_never_runs_old_binary(spec, empty_profiles, tmp_path, step):
    target = tmp_path / "solver"
    target.write_text("#!/usr/bin/env bash\ntouch old-binary-ran\n")
    target.chmod(0o755)
    data = spec.model_dump()
    data.update(run_type="compiled", entrypoint="./solver", prepare_steps=[step],
                run_step={"executable": "./solver", "args": []})
    result = run_bash(render_job_script(JobSpec.model_validate(data), profiles=empty_profiles), tmp_path)
    assert result.returncode != 0
    assert result.stdout == ""
    assert not (tmp_path / "old-binary-ran").exists()


def test_environment_cannot_leave_build_errexit_disabled(spec, empty_profiles, tmp_path):
    empty_profiles.environments[0].load_steps = [
        Command(executable="set", args=["+e"])
    ]
    data = spec.model_dump()
    data["prepare_steps"] = [{"kind": "command", "executable": "false"}]
    result = run_bash(
        render_job_script(JobSpec.model_validate(data), profiles=empty_profiles), tmp_path
    )
    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("target_kind", ["missing", "non_executable", "directory", "executable"])
def test_compiled_target_is_checked_after_build(spec, empty_profiles, tmp_path, target_kind):
    target = tmp_path / "solver"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind != "missing":
        target.write_text("#!/usr/bin/env bash\nprintf compiled-ok\n")
        target.chmod(0o755 if target_kind == "executable" else 0o644)
    data = spec.model_dump()
    data.update(run_type="compiled", entrypoint="./solver",
                prepare_steps=[{"kind": "command", "executable": "true"}],
                run_step={"executable": "./solver"})
    script = render_job_script(JobSpec.model_validate(data), profiles=empty_profiles)
    assert script.index("'true'") < script.index("test -x ./solver") < script.index("# Run")
    result = run_bash(script, tmp_path)
    assert (result.returncode == 0) == (target_kind == "executable")
    assert result.stdout == ("compiled-ok" if target_kind == "executable" else "")


def test_compiled_bare_target_is_ambiguous(spec, empty_profiles):
    data = spec.model_dump()
    data.update(run_type="compiled", entrypoint="solver", run_step={"executable": "solver"},
                prepare_steps=[{"kind": "command", "executable": "true"}])
    with pytest.raises(JobSpecValidationError, match="explicit path"):
        render_job_script(JobSpec.model_validate(data), profiles=empty_profiles)


def test_prepare_work_dir_and_raw_shell_state_are_isolated(spec, empty_profiles, tmp_path):
    build = tmp_path / "build dir"
    build.mkdir()
    data = spec.model_dump()
    data["prepare_steps"] = [
        {"kind": "shell", "work_dir": str(build),
         "script": 'cat > result <<\'EOF\'\nraw $ ; " text\nEOF\ncd /\nexit 0'},
        {"kind": "command", "executable": "touch", "args": ["in-work-dir"]},
    ]
    result = run_bash(render_job_script(JobSpec.model_validate(data), profiles=empty_profiles), tmp_path)
    assert result.returncode == 0, result.stderr
    assert (build / "result").read_text() == 'raw $ ; " text\n'
    assert (tmp_path / "in-work-dir").exists()
    assert json.loads(result.stdout) == []


@pytest.mark.parametrize("launcher", ["srun", "mpirun"])
def test_launchers_are_only_used_when_explicit(spec, empty_profiles, launcher):
    empty_profiles.launchers = [LaunchDefinition(
        id="parallel", version="1", command=Command(executable=launcher, args=["-n", "2"])
    )]
    assert launcher not in render_job_script(spec, profiles=empty_profiles)
    spec.run_step.launcher_profile = ProfileReference(id="parallel", version="1")
    spec.resources.ntasks = 2
    spec.resources.cpus_per_task = 4
    script = render_job_script(spec, profiles=empty_profiles)
    assert "#SBATCH --ntasks=2\n" in script and "#SBATCH --cpus-per-task=4\n" in script
    assert shlex.split(script.split("# Run\n", 1)[1]) == [
        launcher, "-n", "2", spec.run_step.executable, *spec.run_step.args
    ]


@pytest.mark.parametrize("missing", ["environment_id", "environment_version", "launcher"])
def test_unregistered_profiles_are_rejected(spec, empty_profiles, missing):
    if missing == "environment_id":
        spec.environment_profile.id = "unknown"
    elif missing == "environment_version":
        spec.environment_profile.version = "2"
    else:
        spec.run_step.launcher_profile = ProfileReference(id="unknown", version="1")
    with pytest.raises(JobSpecValidationError, match="no static profile"):
        render_job_script(spec, profiles=empty_profiles)


def test_registry_mutations_are_revalidated(spec, empty_profiles):
    empty_profiles.environments.append(empty_profiles.environments[0])
    with pytest.raises(JobSpecValidationError, match="duplicate"):
        render_job_script(spec, profiles=empty_profiles)


def test_unresolved_items_report_field_and_reason(spec, empty_profiles):
    data = spec.model_dump()
    data["unresolved"] = [{"field": "resources", "reason": "needs confirmation"}]
    with pytest.raises(JobSpecValidationError) as caught:
        render_job_script(JobSpec.model_validate(data), profiles=empty_profiles)
    assert caught.value.issues == ("unresolved.resources: needs confirmation",)


def test_rendering_is_pure_and_does_not_infer_inputs(spec, empty_profiles, monkeypatch):
    data = spec.model_dump()
    data["source_fingerprints"] = [{"path": "metadata-only.py", "sha256": "0" * 64}]
    proposal = JobSpec.model_validate(data)
    registry_before = empty_profiles.model_dump()

    def unexpected_io(*args, **kwargs):
        pytest.fail("renderer attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", unexpected_io)
        patch.setattr(Path, "open", unexpected_io)
        patch.setattr(Path, "stat", unexpected_io)
        patch.setattr(subprocess, "Popen", unexpected_io)
        script = render_job_script(proposal, profiles=empty_profiles)
    assert "test -r" not in script
    assert "metadata-only.py" not in script
    assert proposal.model_dump() == data
    assert empty_profiles.model_dump() == registry_before


def test_missing_fields_and_wrong_input_types_are_rejected(spec, empty_profiles):
    invalid = spec.model_dump()
    del invalid["run_step"]
    with pytest.raises(JobSpecValidationError, match="run_step"):
        render_job_script(JobSpec.model_construct(**invalid), profiles=empty_profiles)
    with pytest.raises(JobSpecValidationError, match="expected JobSpec"):
        render_job_script({}, profiles=empty_profiles)
