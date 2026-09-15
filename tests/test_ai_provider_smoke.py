"""Manual smoke entry logic with fake transport only; never a real API test."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket
import subprocess

import pytest

# Load only our own maintained CLI module; no import-time API calls or reliance
# on `python -m pytest` adding the repository root to sys.path.
spec = importlib.util.spec_from_file_location("smoke_ai_provider", Path(__file__).parents[1] / "scripts/smoke_ai_provider.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
from sbatch_agent.model_client import ModelAvailability, ModelErrorCode, ModelResponse, ModelUnavailableError
from sbatch_agent.scanner import ProjectScanner
from test_analyzer import project, evidence, python_output


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Smoke unit tests must not connect or execute programs")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in ("PROVIDER", "MODEL", "ENDPOINT", "API_KEY_ENV", "TIMEOUT"):
        monkeypatch.delenv("SBATCH_AGENT_AI_" + name, raising=False)


class FakeTransport:
    provider, model = "fake", "offline-smoke"
    def __init__(self, responses, ready=True):
        self.responses = responses
        self.calls = []
        self.ready = ready
    def availability(self):
        return ModelAvailability("available" if self.ready else "credential_missing", self.model)
    def generate_structured(self, **kwargs):
        self.calls.append(kwargs)
        output = self.responses.pop(0)
        if isinstance(output, Exception):
            raise output
        return ModelResponse(deepcopy(output))


def inject(monkeypatch, responses, ready=True):
    fake = FakeTransport(responses, ready)
    monkeypatch.setattr(smoke, "model_client_from_env", lambda: fake)
    return fake


def test_missing_configuration_stops_before_network_or_scan(monkeypatch, capsys):
    monkeypatch.setattr(ProjectScanner, "scan", lambda *args: pytest.fail("Must not scan without provider config"))
    assert smoke.main([]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["phase"] == "configuration" and report["error_code"] == "provider_not_configured"


def test_provider_only_probe_contains_no_project_data(monkeypatch, capsys):
    fake = inject(monkeypatch, [{"status": "ok"}])
    monkeypatch.setattr(ProjectScanner, "scan", lambda *args: pytest.fail("Layer one must not scan"))
    assert smoke.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["provider_probe"]["structured_output"] == "passed"
    assert "analysis" not in report and len(fake.calls) == 1
    assert fake.calls[0]["schema"] == smoke.PROBE_SCHEMA
    assert not fake.calls[0]["context"].evidence_refs


@pytest.mark.parametrize("output", [ModelUnavailableError("PRIVATE", code=ModelErrorCode.AUTHENTICATION),
                                   {"status": "not-ok"}, {"status": "ok", "extra": "PRIVATE"}, "not-a-dict"])
def test_failed_provider_probe_blocks_analyzer(monkeypatch, capsys, output):
    fake = inject(monkeypatch, [output])
    monkeypatch.setattr(ProjectScanner, "scan", lambda *args: pytest.fail("Must not scan after failed provider probe"))
    assert smoke.main(["--project-dir", "/unused", "--task-intent", "demo"]) == 2
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["phase"] == "provider" and "analysis" not in report
    assert "PRIVATE" not in stdout and len(fake.calls) == 1


def test_missing_credential_blocks_probe(monkeypatch, capsys):
    fake = inject(monkeypatch, [], ready=False)
    assert smoke.main([]) == 2
    assert json.loads(capsys.readouterr().out)["error_code"] == "credential_missing"
    assert not fake.calls


def test_two_layers_use_existing_analyzer_and_post_validation(monkeypatch, capsys, project, evidence):
    fake = inject(monkeypatch, [{"status": "ok"}, python_output(evidence)])
    assert smoke.main(["--project-dir", str(project), "--task-intent", "运行 case01"]) == 0
    report = json.loads(capsys.readouterr().out)
    fields = report["analysis"]["fields"]
    assert fields["entrypoint"]["value"] == {"kind": "file", "value": "run.py"}
    assert fields["resource_requirements.memory_mib"]["status"] == "UNRESOLVED"
    assert report["analysis"]["manual_review_required"] is True and len(fake.calls) == 2
    assert "UNTRUSTED PROJECT CONTENT" in fake.calls[1]["context"].system
    assert str(project) not in json.dumps(report)


def test_analyzer_smoke_rejects_hallucinated_path(monkeypatch, capsys, project, evidence):
    output = python_output(evidence)
    output["draft"]["entrypoint"]["value"]["value"] = "does-not-exist.py"
    fake = inject(monkeypatch, [{"status": "ok"}, output])
    assert smoke.main(["--project-dir", str(project), "--task-intent", "case01"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["provider_probe"]["structured_output"] == "passed"
    assert report["error_code"] == "invalid_structured_response" and len(fake.calls) == 2
    assert "analysis" not in report


def test_smoke_configuration_error_is_sanitized(monkeypatch, capsys):
    monkeypatch.setenv("SBATCH_AGENT_AI_ENDPOINT", "PRIVATE")
    assert smoke.main([]) == 2
    output = capsys.readouterr().out
    assert "PRIVATE" not in output and "invalid_configuration" in output
