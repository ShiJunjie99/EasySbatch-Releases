"""M3-A: fake CLI responses only; real process creation is forbidden here."""

import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from sbatch_agent import (
    CommandResult, JobSpec, JobState, SlurmClient, SlurmCommandError,
    SlurmParseError, StaticProfiles, SubprocessRunner, render_job_script,
    resolve_log_path,
)


@pytest.fixture(autouse=True)
def forbid_process_creation(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("M3-A tests must not start processes, including real Slurm CLI")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


class FakeRunner:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, *, timeout):
        self.calls.append((list(argv), timeout))
        assert self.responses, "unexpected extra CLI call (or retry)"
        returncode, stdout, stderr = self.responses.pop(0)
        return CommandResult(tuple(argv), returncode, stdout, stderr)


def client_with(*responses):
    runner = FakeRunner(*responses)
    return SlurmClient(runner, timeout=7), runner


@pytest.mark.parametrize("output,job_id,cluster", [
    ("123456\n", "123456", None),
    ("123456", "123456", None),
    ("123456;cluster-name\n", "123456", "cluster-name"),
    ("123456;gpu\r\n", "123456", "gpu"),
])
def test_submit_parsable(output, job_id, cluster, tmp_path):
    client, runner = client_with((0, output, ""))
    path = tmp_path / "submit.sh"
    result = client.submit(path)
    assert result.job_id == job_id
    assert result.cluster_name == cluster
    assert (result.returncode, result.stdout, result.stderr) == (0, output, "")
    assert result.command.argv == ("sbatch", "--parsable", str(path))
    assert runner.calls == [(["sbatch", "--parsable", str(path)], 7)]
    assert not path.exists()  # Adapter does not create or inspect script files.


@pytest.mark.parametrize("stderr", ["sbatch: warning: site notice\n", "error-like diagnostic\n"])
def test_valid_acknowledgement_keeps_stderr_without_keyword_guessing(stderr):
    client, runner = client_with((0, "123\n", stderr))
    assert client.submit("submit.sh").stderr == stderr
    assert len(runner.calls) == 1


@pytest.mark.parametrize("stdout", ["", "123\n"])
def test_submit_nonzero_never_returns_job_id(stdout):
    client, runner = client_with((1, stdout, "sbatch: error: Invalid account\n"))
    with pytest.raises(SlurmCommandError) as caught:
        client.submit("submit.sh")
    assert caught.value.result.returncode == 1
    assert caught.value.result.stdout == stdout
    assert "Invalid account" in caught.value.result.stderr
    assert len(runner.calls) == 1


@pytest.mark.parametrize("stdout", [
    "", "\n", "  ", "abc", "0", "-1", "1.5", "１２３", "00123", "123.batch",
    "123_1", "123;", "123;gpu;extra", "123;gpu cluster", "123;$(touch marker)",
    "123;gpu\n456", "123\n456", "banner\n123\n", "Submitted batch job 123\n",
    " 123\n", "123 \n", "123\n\n", "123\x00", "123\rjunk",
])
def test_malformed_acknowledgement_retains_raw_response(stdout):
    client, runner = client_with((0, stdout, "diagnostic"))
    with pytest.raises(SlurmParseError) as caught:
        client.submit("submit.sh")
    assert caught.value.result.stdout == stdout
    assert caught.value.result.stderr == "diagnostic"
    assert caught.value.result.returncode == 0
    assert len(runner.calls) == 1


@pytest.mark.parametrize("path", ["", "  ", "bad\nfile", "bad\rfile", "bad\x00file"])
def test_invalid_script_path_fails_before_execution(path):
    client, runner = client_with()
    with pytest.raises(ValueError):
        client.submit(path)
    assert not runner.calls


def test_script_path_is_one_absolute_argument_and_cannot_be_an_option(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = "--wrap=touch marker; $(touch x) ' \" & (script).sh"
    client, runner = client_with((0, "123\n", ""))
    client.submit(name)
    assert runner.calls[0][0] == ["sbatch", "--parsable", str(tmp_path / name)]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("state,reason", [
    ("PENDING", "Resources"), ("PENDING", "Priority"), ("RUNNING", "None"),
    ("COMPLETING", "None"), ("SUSPENDED", "None"),
])
def test_queue_fields_and_no_accounting_call_when_found(state, reason):
    output = f"123|{state}|{reason}|intel_2080|alice\n"
    client, runner = client_with((0, output, "queue warning"))
    status = client.get_status("123")
    assert status.job_id == "123"
    assert status.normalized_state == state
    assert status.raw_state == state
    assert status.reason == reason
    assert status.partition == "intel_2080"
    assert status.user == "alice"
    assert status.source == "squeue"
    assert status.exit_code is None and status.signal is None
    assert status.query_results[0].stdout == output
    assert status.query_results[0].stderr == "queue warning"
    assert runner.calls == [([
        "squeue", "--local", "--noheader", "--states=all",
        "--jobs=123", "--format=%i|%T|%r|%P|%u",
    ], 7)]


@pytest.mark.parametrize("state,raw_exit,code,signal,normalized", [
    ("COMPLETED", "0:0", 0, 0, JobState.COMPLETED),
    ("FAILED", "1:0", 1, 0, JobState.FAILED),
    ("CANCELLED", "0:9", 0, 9, JobState.CANCELLED),
    ("CANCELLED by 1000", "0:15", 0, 15, JobState.CANCELLED),
    ("TIMEOUT", "0:9", 0, 9, JobState.TIMEOUT),
    ("OUT_OF_MEMORY", "0:9", 0, 9, JobState.FAILED),
    ("BOOT_FAIL", "1:0", 1, 0, JobState.FAILED),
    ("NODE_FAIL", "0:9", 0, 9, JobState.FAILED),
    ("DEADLINE", "0:0", 0, 0, JobState.FAILED),
    ("CONFIGURING", "0:0", 0, 0, JobState.PENDING),
])
def test_accounting_fallback_and_exit_code(state, raw_exit, code, signal, normalized):
    start, end = "2026-09-03T20:19:18", "2026-09-03T20:19:21"
    output = f"123|{state}|{raw_exit}|{start}|{end}\n"
    client, runner = client_with((0, "", ""), (0, output, "accounting warning"))
    status = client.get_status("123")
    assert status.source == "sacct"
    assert status.normalized_state is normalized
    assert status.raw_state == state
    assert (status.exit_code, status.signal, status.raw_exit_code) == (code, signal, raw_exit)
    assert (status.start, status.end) == (start, end)
    assert status.partition is None and status.user is None
    assert [result.stdout for result in status.query_results] == ["", output]
    assert status.query_results[1].stderr == "accounting warning"
    assert runner.calls[1] == ([
        "sacct", "--local", "--noheader", "--parsable2", "--allocations",
        "--jobs=123", "--format=JobID,State%80,ExitCode,Start,End",
    ], 7)


def test_not_in_either_source_is_unknown_not_completed():
    client, runner = client_with((0, "\n", ""), (0, "", ""))
    status = client.get_status("123")
    assert status.normalized_state is JobState.UNKNOWN
    assert status.raw_state is None
    assert status.source == "none"
    assert status.raw_exit_code is None and status.exit_code is None
    assert len(status.query_results) == len(runner.calls) == 2


def test_specific_purged_job_diagnostic_falls_back_without_masking_it():
    diagnostic = "slurm_load_jobs error: Invalid job id specified\n"
    client, runner = client_with(
        (1, "", diagnostic), (0, "123|COMPLETED|0:0|start|end\n", "")
    )
    status = client.get_status("123")
    assert status.normalized_state is JobState.COMPLETED
    assert status.query_results[0].returncode == 1
    assert status.query_results[0].stderr == diagnostic
    assert len(runner.calls) == 2


@pytest.mark.parametrize("stderr", [
    "Unable to contact slurm controller", "Permission denied",
    "slurm_load_jobs error: Invalid job id specified\nother error",
])
def test_queue_failure_is_not_empty_queue(stderr):
    client, runner = client_with((1, "", stderr))
    with pytest.raises(SlurmCommandError) as caught:
        client.get_status("123")
    assert caught.value.result.stderr == stderr
    assert len(runner.calls) == 1


def test_accounting_service_failure_is_not_no_records():
    client, runner = client_with((0, "", ""), (1, "", "slurmdbd unavailable"))
    with pytest.raises(SlurmCommandError) as caught:
        client.get_status("123")
    assert caught.value.result.argv[0] == "sacct"
    assert caught.value.result.stderr == "slurmdbd unavailable"
    assert len(runner.calls) == 2


@pytest.mark.parametrize("main_first", [True, False])
def test_steps_never_override_main_record(main_first):
    main = "123|FAILED|1:0|start|end\n"
    steps = "123.batch|COMPLETED|0:0|start|end\n123.extern|COMPLETED|0:0|start|end\n"
    output = main + steps if main_first else steps + main
    client, _ = client_with((0, "", ""), (0, output, ""))
    status = client.get_status("123")
    assert status.normalized_state is JobState.FAILED
    assert status.exit_code == 1


def test_only_steps_or_other_jobs_does_not_imply_main_job_completed():
    queue = "1234|RUNNING|None|cpu|user\n123_[1-3]|PENDING|Priority|cpu|user\n"
    accounting = "123.batch|COMPLETED|0:0|start|end\n123.extern|COMPLETED|0:0|start|end\n"
    client, _ = client_with((0, queue, ""), (0, accounting, ""))
    assert client.get_status("123").source == "none"


@pytest.mark.parametrize("raw", ["FUTURE_STATE", "CANCELLED+", "COMPLETED_NEW_FLAG"])
@pytest.mark.parametrize("source", ["squeue", "sacct"])
def test_unknown_state_preserves_raw_without_crashing(raw, source):
    response = (0, f"123|{raw}|Priority|cpu|user\n", "") if source == "squeue" else (
        0, f"123|{raw}|0:0|Unknown|Unknown\n", ""
    )
    responses = [response] if source == "squeue" else [(0, "", ""), response]
    client, _ = client_with(*responses)
    status = client.get_status("123")
    assert status.normalized_state is JobState.UNKNOWN
    assert status.raw_state == raw
    assert status.source == source


@pytest.mark.parametrize("raw_exit", ["0:0", ""])
def test_pending_accounting_is_not_success_and_missing_exit_code_is_not_zero(raw_exit):
    client, _ = client_with((0, "", ""), (0, f"123|PENDING|{raw_exit}|Unknown|Unknown\n", ""))
    status = client.get_status("123")
    assert status.normalized_state is JobState.PENDING
    assert status.start == status.end == "Unknown"
    assert status.raw_exit_code == raw_exit
    assert status.exit_code == (0 if raw_exit else None)


@pytest.mark.parametrize("raw_exit", ["0", "a:b", "-1:0", "0:9:2", "0:x"])
def test_bad_exit_code_is_a_parse_error(raw_exit):
    client, _ = client_with((0, "", ""), (0, f"123|FAILED|{raw_exit}|start|end\n", ""))
    with pytest.raises(SlurmParseError, match="ExitCode"):
        client.get_status("123")


@pytest.mark.parametrize("source", ["squeue", "sacct"])
@pytest.mark.parametrize("output", [
    "unexpected banner\n", "123|RUNNING|short\n", "123||None|cpu|user\n",
    "123|RUNNING|extra|delimiter|cpu|user\n",
    "123|RUNNING|0:0|start|end\n123|FAILED|1:0|start|end\n",
])
def test_malformed_or_duplicate_status_records_are_not_silent_absence(source, output):
    responses = [(0, output, "")] if source == "squeue" else [(0, "", ""), (0, output, "")]
    client, _ = client_with(*responses)
    with pytest.raises(SlurmParseError) as caught:
        client.get_status("123")
    assert caught.value.result.stdout == output


@pytest.mark.parametrize("job_id", [
    "", "0", "-1", "00123", "１２３", 123, True, None, "123_1", "123.batch", "123,124",
    "123;touch marker", "$(touch marker)", "123\n456", "123\r", "--help", "123;gpu",
])
def test_job_id_rejected_before_any_cli_call(job_id):
    client, runner = client_with()
    with pytest.raises(ValueError, match="job_id"):
        client.get_status(job_id)
    assert runner.calls == []


def test_subprocess_boundary_preserves_argv_output_timeout_and_environment(monkeypatch):
    mock = Mock(return_value=subprocess.CompletedProcess([], 4, "out\n", "err\n"))
    monkeypatch.setattr(subprocess, "run", mock)
    monkeypatch.setenv("LC_ALL", "original")
    monkeypatch.setenv("SLURM_TIME_FORMAT", "relative")
    argv = ["sbatch", "--parsable", "/tmp/one argument; ' $ & (x).sh"]
    result = SubprocessRunner().run(argv, timeout=2.5)
    assert result == CommandResult(tuple(argv), 4, "out\n", "err\n")
    positional, kwargs = mock.call_args
    assert positional == (argv,)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 2.5
    assert kwargs["capture_output"] is True and kwargs["check"] is False
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["env"]["LC_ALL"] == "C"
    assert kwargs["env"]["SLURM_TIME_FORMAT"] == "standard"
    assert os.environ["LC_ALL"] == "original"
    assert os.environ["SLURM_TIME_FORMAT"] == "relative"


@pytest.mark.parametrize("command", ["sbatch", "squeue", "sacct", "sinfo"])
def test_backend_credential_and_configuration_never_reach_slurm(monkeypatch, command):
    mock = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", mock)
    monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", "M7_CUSTOM_PROVIDER_TOKEN")
    monkeypatch.setenv("M7_CUSTOM_PROVIDER_TOKEN", "offline-test-only")
    monkeypatch.setenv("SBATCH_AGENT_LOCAL_AI_KEY", "offline-local-only")
    monkeypatch.setenv("SBATCH_AGENT_DATABASE_PATH", "/backend/private.sqlite3")
    monkeypatch.setenv("SBATCH_AGENT_AI_ENDPOINT", "https://provider.invalid")
    monkeypatch.setenv("PATH", "/ordinary/bin:/usr/bin")
    monkeypatch.setenv("OMP_NUM_THREADS", "2")
    before = dict(os.environ)
    SubprocessRunner().run([command, "--version"], timeout=1)
    child = mock.call_args.kwargs["env"]
    assert "M7_CUSTOM_PROVIDER_TOKEN" not in child
    assert not any(key.startswith("SBATCH_AGENT_") for key in child)
    assert child["PATH"] == before["PATH"] and child["OMP_NUM_THREADS"] == "2"
    assert os.environ == before  # AI stays available; no process-global mutation.


@pytest.mark.parametrize("credential_name", [None, "M7_MISSING_TOKEN"])
def test_unconfigured_ai_does_not_change_ordinary_slurm_environment(monkeypatch, credential_name):
    mock = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", mock)
    monkeypatch.delenv("SBATCH_AGENT_AI_API_KEY_ENV", raising=False)
    monkeypatch.delenv("M7_MISSING_TOKEN", raising=False)
    if credential_name:
        monkeypatch.setenv("SBATCH_AGENT_AI_API_KEY_ENV", credential_name)
    before = dict(os.environ)
    SubprocessRunner().run(["sbatch", "--version"], timeout=1)
    expected = {key: value for key, value in before.items() if not key.startswith("SBATCH_AGENT_")}
    expected.update(LC_ALL="C", SLURM_TIME_FORMAT="standard")
    assert mock.call_args.kwargs["env"] == expected
    assert os.environ == before


@pytest.mark.parametrize("failure", [FileNotFoundError("missing CLI"), PermissionError("denied")])
@pytest.mark.parametrize("method,arg", [("submit", "submit.sh"), ("get_status", "123")])
def test_os_failure_becomes_command_error_without_retry(monkeypatch, failure, method, arg):
    mock = Mock(side_effect=failure)
    monkeypatch.setattr(subprocess, "run", mock)
    with pytest.raises(SlurmCommandError) as caught:
        getattr(SlurmClient(), method)(arg)
    assert caught.value.__cause__ is failure
    assert caught.value.result.returncode is None
    assert caught.value.result.stdout == caught.value.result.stderr == ""
    assert mock.call_count == 1


@pytest.mark.parametrize("partial", [(b"123\n", b"partial\xff"), (None, None)])
def test_timeout_keeps_partial_output_and_does_not_retry(monkeypatch, partial):
    failure = subprocess.TimeoutExpired(["sbatch"], 3, output=partial[0], stderr=partial[1])
    mock = Mock(side_effect=failure)
    monkeypatch.setattr(subprocess, "run", mock)
    with pytest.raises(SlurmCommandError, match="timed out") as caught:
        SlurmClient(timeout=3).submit("submit.sh")
    assert caught.value.__cause__ is failure
    assert caught.value.result.returncode is None
    assert caught.value.result.stdout == ("123\n" if partial[0] else "")
    assert caught.value.result.stderr == ("partial\ufffd" if partial[1] else "")
    assert mock.call_count == 1


@pytest.mark.parametrize("timeout", [0, -1, True, "30", float("inf"), float("nan")])
def test_invalid_timeout_rejected(timeout):
    with pytest.raises(ValueError, match="timeout"):
        SlurmClient(timeout=timeout)
    with pytest.raises(ValueError, match="timeout"):
        SubprocessRunner().run(["sbatch"], timeout=timeout)


@pytest.mark.parametrize("argv", ["sbatch submit.sh", [], [""], ["sbatch", 3], ["sbatch", "\x00"]])
def test_runner_rejects_shell_strings_or_invalid_argv(argv):
    with pytest.raises(ValueError, match="argv"):
        SubprocessRunner().run(argv, timeout=1)


@pytest.mark.parametrize("pattern,expected", [
    ("smoke-%j.out", "/work dir/smoke-123.out"),
    ("logs/%j-%j.err", "/work dir/logs/123-123.err"),
    ("/absolute/%j.out", "/absolute/123.out"),
    ("log ' \" $ ; & (x).out", "/work dir/log ' \" $ ; & (x).out"),
    (None, None),
])
def test_log_path_resolution_is_literal_and_does_not_guess_defaults(pattern, expected):
    assert resolve_log_path(pattern, "123", work_dir="/work dir") == expected


@pytest.mark.parametrize("pattern", ["%A_%a.out", "%N.out", "%05j.out", "%%.out", "%", r"\%j", "", "\n"])
def test_unsupported_log_patterns_are_rejected(pattern):
    with pytest.raises(ValueError):
        resolve_log_path(pattern, "123", work_dir="/work")


def test_log_resolution_requires_safe_job_id_and_absolute_work_dir():
    with pytest.raises(ValueError, match="job_id"):
        resolve_log_path("%j.out", "1/../../x", work_dir="/work")
    with pytest.raises(ValueError, match="work_dir"):
        resolve_log_path("%j.out", "123", work_dir="relative")


@pytest.mark.parametrize("name", ["python", "compiled", "installed"])
def test_existing_spec_to_renderer_to_fake_submission(name, tmp_path):
    root = Path(__file__).resolve().parents[1]
    spec = JobSpec.model_validate(yaml.safe_load(
        (root / f"examples/rendering/{name}.yaml").read_text()
    ))
    profiles = StaticProfiles.model_validate(yaml.safe_load(
        (root / "examples/profiles.yaml").read_text()
    ))
    before = spec.model_dump()
    script = render_job_script(spec, profiles=profiles)
    path = tmp_path / "submit.sh"
    path.write_text(script)
    client, runner = client_with((0, "123;gpu\n", ""))
    submission = client.submit(path)
    expected_stdout = resolve_log_path(spec.stdout, submission.job_id, work_dir=spec.work_dir)
    if spec.stdout is None:
        assert expected_stdout is None
    else:
        assert expected_stdout and "%j" not in expected_stdout
    assert path.read_text() == script
    assert spec.model_dump() == before
    assert len(runner.calls) == 1
    assert "--account" not in runner.calls[0][0] and "--qos" not in runner.calls[0][0]
