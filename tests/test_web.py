"""Browser-facing lifecycle with real temporary SQLite and fake Slurm only."""

from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from sbatch_agent import (
    CommandResult, JobRepository, JobState, JobStatus, PersistenceError,
    SlurmClient, SlurmCommandError, StaticProfiles, SubmissionResult,
    SubmissionService, SubmissionState, SubprocessRunner,
)
from sbatch_agent.web import MAX_FORM_BYTES, WebConfig, create_app
from sbatch_agent.web_forms import DEFAULT_FORM, profile_key


@pytest.fixture(autouse=True)
def no_real_slurm(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Web tests must not execute any process or real Slurm command")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(SubprocessRunner, "run", forbidden)
    monkeypatch.setattr(SlurmClient, "submit", forbidden)
    monkeypatch.setattr(SlurmClient, "get_status", forbidden)


def observation(state, *, raw=None):
    completed = state is JobState.COMPLETED
    return JobStatus(
        "123", state, raw or state.value, "sacct" if completed else "squeue", (),
        reason="Priority" if state is JobState.PENDING else None,
        exit_code=0 if completed else None, signal=0 if completed else None,
        raw_exit_code="0:0" if completed else None,
        start="2026-01-01T00:00:00" if completed else None,
        end="2026-01-01T00:00:10" if completed else None,
    )


class FakeSlurmClient:
    def __init__(self):
        self.submit_calls = []
        self.status_calls = []
        self.failure = None
        self.states = []
        self.before_submit = None

    def submit(self, script_path):
        self.submit_calls.append(Path(script_path))
        if self.before_submit:
            self.before_submit(Path(script_path))
        if self.failure:
            raise self.failure
        return SubmissionResult("123", "offline-cluster", CommandResult(
            ("fake-submit", str(script_path)), 0, "123;offline-cluster\n", "",
        ))

    def get_status(self, job_id):
        self.status_calls.append(job_id)
        assert self.states, "Unexpected polling/retry"
        result = self.states.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def profiles():
    return StaticProfiles.model_validate({
        "environments": [
            {"id": "system-python", "version": "1", "load_steps": []},
            {"id": "gcc", "version": "12", "load_steps": [
                {"executable": "module", "args": ["load", "gcc/12"]},
            ]},
            {"id": "software", "version": "2", "load_steps": [
                {"executable": "module", "args": ["load", "example-software"]},
            ]},
        ],
        "launchers": [{"id": "explicit-launch", "version": "1", "command": {
            "executable": "mpirun", "args": ["-np", "2"],
        }}],
    })


@pytest.fixture
def config(tmp_path):
    return WebConfig(tmp_path / "data" / "jobs.sqlite3", tmp_path / "runs")


@pytest.fixture
def fake():
    return FakeSlurmClient()


@pytest.fixture
def browser(config, profiles, fake):
    with TestClient(create_app(config, profiles=profiles, slurm_client=fake),
                    base_url="http://127.0.0.1") as client:
        yield client


@pytest.fixture
def form(tmp_path, profiles):
    return {**DEFAULT_FORM,
            "name": "web-demo", "project_dir": str(tmp_path / "project with spaces"),
            "work_dir": str(tmp_path / "work"), "entrypoint": "hello.py",
            "environment": profile_key(profiles.environments[0]),
            "executable": "/usr/bin/python3", "args": '["hello.py"]',
            "partition": "offline_cpu", "memory_mib": "256", "time_limit_seconds": "120",
            "memory_mode": "explicit", "walltime_mode": "explicit",
            "stdout": "logs/demo-%j.out", "stderr": "logs/demo-%j.err"}


def token(browser, page="/new"):
    response = browser.get(page)
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text)[1]


def post(browser, path, data=None):
    return browser.post(path, data={**(data or {}), "csrf_token": token(browser)},
                        follow_redirects=False)


def create(browser, form):
    response = post(browser, "/new", form)
    assert response.status_code == 303, response.text
    return response.headers["location"]


def saved(config, url):
    with JobRepository(config.database_path) as repository:
        return repository.get(url.rsplit("/", 1)[1])


def test_home_identity_and_empty_list_do_not_query(browser, fake, monkeypatch):
    monkeypatch.setenv("USER", "forged-identity")
    response = browser.get("/")
    assert response.status_code == 200
    assert '新建任务' in response.text and '任务记录' in response.text
    assert f"<strong>{pwd.getpwuid(os.geteuid()).pw_name}</strong>" in response.text
    assert "forged-identity" not in response.text
    assert "暂无任务记录" in browser.get("/jobs").text
    assert not fake.submit_calls and not fake.status_calls


@pytest.mark.parametrize("kind, environment, executable, args, steps, expected", [
    ("python", 0, "/usr/bin/python3", ["hello.py", "case 01.json"], [], "hello.py 'case 01.json'"),
    ("compiled", 1, "./build/solver", ["--input", "case.json"],
     [{"kind": "command", "executable": "make", "args": []}], "test -x ./build/solver"),
    ("installed", 2, "software_binary", ["--input", "case.json"], [], "'software_binary' --input case.json"),
])
def test_create_three_modes_via_service(browser, config, form, profiles, fake, monkeypatch,
                                      kind, environment, executable, args, steps, expected):
    calls = []
    original = SubmissionService.create_job
    def traced(service, **kwargs):
        calls.append(kwargs["spec"])
        return original(service, **kwargs)
    monkeypatch.setattr(SubmissionService, "create_job", traced)
    form.update(run_type=kind, environment=profile_key(profiles.environments[environment]),
                executable=executable, args=json.dumps(args), prepare_steps=json.dumps(steps))
    url = create(browser, form)
    record = saved(config, url)
    assert len(calls) == 1 and calls[0] == record.job_spec
    assert record.job_spec.run_type == kind
    assert record.job_spec.run_step.args == args
    assert expected in record.rendered_script
    assert record.submission_state is SubmissionState.SCRIPT_RENDERED
    assert record.job_spec.resources.gpus is None
    assert not config.runs_root.exists()  # creation only persists snapshots
    response = browser.get(url)
    assert '任务配置快照（JobSpec）' in response.text and '查看已保存脚本' in response.text
    assert "SCRIPT_RENDERED" in response.text and "尚未查询" in response.text
    assert f'action="{url}/submit"' in response.text
    assert f'action="{url}/refresh"' not in response.text
    assert record.job_spec_snapshot in unescape(response.text)
    assert record.rendered_script in unescape(response.text)
    assert "srun" not in record.rendered_script
    assert not fake.submit_calls and not fake.status_calls


@pytest.mark.parametrize("updates, message", [
    ({"time_limit_seconds": "00:60:00"}, "运行时限"),
    ({"memory_mib": "0"}, "memory_mib"),
    ({"entrypoint": ""}, "entrypoint"),
    ({"name": ""}, "任务名称不能为空"),
    ({"environment": '["missing", "1"]'}, "运行环境 未找到"),
    ({"launcher": '["missing", "1"]'}, "启动配置 未找到"),
    ({"run_type": "compiled"}, "prepare_step"),
    ({"args": '--input "unclosed'}, "程序参数格式不正确"),
    ({"args": "{}"}, "JSON 数组"),
    ({"args": '[1]'}, "args"),
    ({"args": '["\\ud800"]'}, "JSON"),
    ({"required_inputs": '[""]'}, "required_inputs"),
    ({"partition": "cpu\n#SBATCH --nodes=99"}, "partition"),
    ({"work_dir": "relative"}, "work_dir"),
    ({"gpu_type": "a100", "gpu_count": "0"}, "GPU"),
    ({"ntasks": "2"}, "launcher_profile"),
    ({"unresolved": '[{"field":"entrypoint","reason":"unknown"}]'}, "entrypoint"),
    ({"username": "root"}, "不支持的字段"),
])
def test_invalid_create_retains_form_without_record(browser, config, form, fake, updates, message):
    form.update(updates)
    response = post(browser, "/new", form)
    assert response.status_code == 400
    assert message in unescape(response.text)
    assert form["project_dir"] in unescape(response.text)
    assert form["environment"] in unescape(response.text)
    assert "Traceback" not in response.text
    with JobRepository(config.database_path) as repository:
        assert repository.list() == []
    assert not fake.submit_calls and not fake.status_calls


def test_advanced_resources_profiles_and_metadata(browser, config, form, profiles):
    form.update(account="group", qos="normal", nodes="1", ntasks="2", gpu_count="2",
                gpu_type="a100", launcher=profile_key(profiles.launchers[0]), spec_version="7",
                required_inputs='["data with spaces.json"]',
                evidence='[{"field":"entrypoint","source_file":"README","kind":"direct","detail":"manual"}]',
                source_fingerprints=json.dumps([{"path": "hello.py", "sha256": "a" * 64}]))
    record = saved(config, create(browser, form))
    assert record.spec_version == 7
    assert record.job_spec.resources.account == "group"
    assert record.job_spec.resources.qos == "normal"
    assert record.job_spec.resources.gpus.count == 2
    assert record.job_spec.run_step.launcher_profile.id == "explicit-launch"
    assert "'mpirun' -np 2" in record.rendered_script
    assert "--gres=gpu:a100:2" in record.rendered_script
    assert "test -r 'data with spaces.json'" in record.rendered_script
    assert record.job_spec.evidence[0].detail == "manual"
    assert len(record.job_spec.source_fingerprints) == 1


@pytest.mark.parametrize("argument", ["case 01.json", "it's a file", 'a"b', "$HOME", "x; touch bad",
                                      "a & b", "(a)", "line\nnext", "", "中文", "</pre><script>alert(1)</script>"])
def test_arguments_are_literal_and_snapshots_html_escaped(browser, config, form, argument):
    form["args"] = json.dumps(["hello.py", argument])
    url = create(browser, form)
    record = saved(config, url)
    response = browser.get(url)
    assert record.job_spec.run_step.args == ["hello.py", argument]
    assert record.rendered_script in unescape(response.text)
    assert "<script>" not in response.text


def test_complete_offline_web_lifecycle_and_duplicate_submission(browser, config, form, fake, monkeypatch):
    url = create(browser, form)
    original = saved(config, url)
    service_calls = []
    original_submit, original_refresh = SubmissionService.submit_job, SubmissionService.refresh_status
    def traced_submit(service, record_id):
        service_calls.append("submit")
        return original_submit(service, record_id)
    def traced_refresh(service, record_id):
        service_calls.append("refresh")
        return original_refresh(service, record_id)
    monkeypatch.setattr(SubmissionService, "submit_job", traced_submit)
    monkeypatch.setattr(SubmissionService, "refresh_status", traced_refresh)
    def before_submit(path):
        assert saved(config, url).submission_state is SubmissionState.SUBMITTING
        assert path.read_text(encoding="utf-8") == original.rendered_script
    fake.before_submit = before_submit
    response = post(browser, url + "/submit")
    assert response.status_code == 303 and response.headers["location"] == url
    detail = browser.get(url).text
    assert "123" in detail and "SUBMITTED" in detail and "尚未查询" in detail
    assert f'action="{url}/submit"' not in detail and f'action="{url}/refresh"' not in detail
    jobs = browser.get("/jobs").text
    assert f'action="{url}/refresh"' in jobs and 'name="return_to" value="jobs"' in jobs
    assert post(browser, url + "/submit").status_code == 303
    assert "不能再次提交" in browser.get(url).text
    assert len(fake.submit_calls) == 1
    fake.states = [observation(s) for s in [JobState.PENDING, JobState.RUNNING, JobState.COMPLETED]]
    for state in (JobState.PENDING, JobState.RUNNING, JobState.COMPLETED):
        assert post(browser, url + "/refresh").status_code == 303
        assert saved(config, url).normalized_slurm_state is state
        detail = browser.get(url).text
        assert state.value in detail
        if state is JobState.PENDING:
            assert "Priority" in detail
    final = saved(config, url)
    assert final.submission_state is SubmissionState.SUBMITTED
    assert final.slurm_job_id == "123" and final.exit_code == final.signal == 0
    assert "0:0" in detail and final.job_status.end in detail
    assert final.job_spec_snapshot == original.job_spec_snapshot
    assert final.rendered_script == original.rendered_script
    assert final.submit_stdout == "123;offline-cluster\n"
    assert service_calls == ["submit", "submit", "refresh", "refresh", "refresh"]
    assert fake.status_calls == ["123"] * 3
    for _ in range(2):
        assert browser.get(url).status_code == browser.get("/jobs").status_code == 200
    assert len(fake.status_calls) == 3  # GET/F5 does not refresh or resubmit


def test_jobs_list_refresh_returns_to_jobs_and_detail_has_no_refresh_button(
        browser, form, fake):
    url = create(browser, form)
    post(browser, url + "/submit")
    fake.states = [observation(JobState.RUNNING)]
    response = post(browser, url + "/refresh", {"return_to": "jobs"})
    assert response.status_code == 303 and response.headers["location"] == "/jobs"
    jobs = browser.get("/jobs")
    assert "已保存本次查询结果" in jobs.text and "RUNNING" in jobs.text
    assert f'action="{url}/refresh"' in jobs.text
    assert f'action="{url}/refresh"' not in browser.get(url).text


@pytest.mark.parametrize("state", [JobState.UNKNOWN, JobState.FAILED])
def test_unknown_and_failed_slurm_states_are_not_submission_failures(browser, config, form, fake, state):
    url = create(browser, form)
    post(browser, url + "/submit")
    fake.states = [observation(state, raw="FUTURE_STATE" if state is JobState.UNKNOWN else "FAILED")]
    post(browser, url + "/refresh")
    record = saved(config, url)
    assert record.normalized_slurm_state is state
    assert record.submission_state is SubmissionState.SUBMITTED and record.slurm_job_id == "123"
    response = browser.get(url)
    assert response.status_code == 200 and state.value in response.text
    assert record.raw_slurm_state in response.text
    assert len(fake.submit_calls) == 1


@pytest.mark.parametrize("state", [SubmissionState.SUBMISSION_UNKNOWN, SubmissionState.SUBMITTING,
                                   SubmissionState.SUBMITTED, SubmissionState.SUBMIT_FAILED])
def test_nonfresh_records_hide_submit_and_refuse_post(browser, config, form, fake, state):
    url = create(browser, form)
    with JobRepository(config.database_path) as repository:
        if state is SubmissionState.SUBMITTED:
            repository.update_submission(url.split("/")[-1], SubmissionResult(
                "123", None, CommandResult(("fake-submit",), 0, "123\n", ""),
            ))
        else:
            repository.update_submission(url.split("/")[-1], state=state,
                                         **({} if state is SubmissionState.SUBMITTING else {"error_message": "offline"}))
    response = browser.get(url)
    assert f'action="{url}/submit"' not in response.text
    if state is SubmissionState.SUBMISSION_UNKNOWN:
        assert "提交结果无法确认，请先核对 Slurm" in response.text
        assert '勿重复提交' in response.text
    assert post(browser, url + "/submit").status_code == 303
    assert not fake.submit_calls and not fake.status_calls


@pytest.mark.parametrize("failure, expected_state", [
    (SlurmCommandError("synthetic failure", CommandResult(("fake",), 1, "", "invalid account")),
     SubmissionState.SUBMIT_FAILED),
    (RuntimeError("sensitive-internal-detail"), SubmissionState.SUBMISSION_UNKNOWN),
])
def test_submission_errors_redirect_with_safe_notice(browser, config, form, fake, failure, expected_state):
    url = create(browser, form)
    original = saved(config, url)
    fake.failure = failure
    assert post(browser, url + "/submit").status_code == 303
    response = browser.get(url)
    assert expected_state.value in response.text
    assert "sensitive-internal-detail" not in response.text and "Traceback" not in response.text
    record = saved(config, url)
    assert record.submission_state is expected_state
    assert record.slurm_job_id is None
    assert str(failure) in record.submission_error
    assert record.rendered_script == original.rendered_script
    post(browser, url + "/submit")
    assert len(fake.submit_calls) == 1


def test_refresh_without_job_and_query_failure_keep_record(browser, config, form, fake):
    url = create(browser, form)
    post(browser, url + "/refresh")
    assert "无法刷新状态" in browser.get(url).text and not fake.status_calls
    post(browser, url + "/submit")
    fake.states = [observation(JobState.RUNNING), RuntimeError("private diagnostic")]
    post(browser, url + "/refresh")
    before = saved(config, url)
    assert post(browser, url + "/refresh").status_code == 303
    response = browser.get(url)
    assert "状态查询失败" in response.text and "private diagnostic" not in response.text
    assert saved(config, url) == before


def test_restart_detail_and_refresh_use_sqlite_snapshot(config, profiles, fake, form):
    with TestClient(create_app(config, profiles=profiles, slurm_client=fake), base_url="http://localhost") as first:
        url = create(first, form)
        post(first, url + "/submit")
    before = saved(config, url)
    Path(before.script_path).write_text("external file modified", encoding="utf-8")
    second_fake = FakeSlurmClient()
    second_fake.states = [observation(JobState.RUNNING)]
    with TestClient(create_app(config, profiles=StaticProfiles(), slurm_client=second_fake),
                    base_url="http://localhost") as second:
        response = second.get(url)
        assert response.status_code == 200
        assert before.rendered_script in unescape(response.text)
        assert "external file modified" not in response.text
        post(second, url + "/refresh")
        assert "RUNNING" in second.get(url).text
        post(second, url + "/submit")
    assert not second_fake.submit_calls
    assert second_fake.status_calls == ["123"]
    assert saved(config, url).job_spec_snapshot == before.job_spec_snapshot


def test_list_newest_first_with_links_and_escaped_names(browser, form, fake):
    urls = []
    for name in ("older", "<b>newer</b>"):
        urls.append(create(browser, {**form, "name": name}))
    response = browser.get("/jobs")
    assert response.text.index(urls[1]) < response.text.index(urls[0])
    assert "&lt;b&gt;newer&lt;/b&gt;" in response.text
    assert "<b>newer</b>" not in response.text
    assert not fake.submit_calls and not fake.status_calls


@pytest.mark.parametrize("suffix", ["", "/submit", "/refresh"])
def test_not_found(browser, fake, suffix):
    url = f"/jobs/{uuid4()}{suffix}"
    response = post(browser, url) if suffix else browser.get(url)
    assert response.status_code == 404 and "任务记录不存在" in response.text
    assert not fake.submit_calls and not fake.status_calls


def test_invalid_id_does_not_become_a_file_path(browser, fake):
    assert browser.get("/jobs/not-a-uuid").status_code == 404
    assert post(browser, "/jobs/not-a-uuid/submit").status_code == 404
    assert not fake.submit_calls


@pytest.mark.parametrize("action", ["/new", "submit", "refresh"])
@pytest.mark.parametrize("csrf", ["", "wrong-token"])
def test_missing_or_invalid_csrf_cannot_mutate(browser, config, form, fake, action, csrf):
    url = create(browser, form)
    before = saved(config, url)
    path = action if action == "/new" else url + "/" + action
    response = browser.post(path, data={**form, "csrf_token": csrf})
    assert response.status_code == 403
    assert saved(config, url) == before
    with JobRepository(config.database_path) as repository:
        assert len(repository.list()) == 1
    assert not fake.submit_calls and not fake.status_calls


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.example"}, {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
])
def test_cross_origin_post_rejected_even_with_token(browser, form, headers):
    response = browser.post("/new", data={**form, "csrf_token": token(browser)}, headers=headers)
    assert response.status_code == 403


def test_same_origin_post_and_security_headers(browser, form):
    response = browser.get("/new")
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert browser.post("/new", data={**form, "csrf_token": token(browser)},
                        headers={"Origin": "http://127.0.0.1"}, follow_redirects=False).status_code == 303
    assert browser.get("/", headers={"Host": "untrusted.example"}).status_code == 400


def test_csrf_is_bound_to_cookie_session(config, profiles, fake, browser, form):
    stolen = token(browser)
    with TestClient(browser.app, base_url="http://127.0.0.1") as other:
        token(other)
        assert other.post("/new", data={**form, "csrf_token": stolen}).status_code == 403


def test_each_repository_operation_closes_in_the_same_thread(config, profiles, fake, form, monkeypatch):
    from threading import get_ident
    opened, closed = [], []
    original_init, original_close = JobRepository.__init__, JobRepository.close
    def track_init(repository, *args, **kwargs):
        original_init(repository, *args, **kwargs)
        opened.append((repository, get_ident()))
    def track_close(repository):
        closed.append((repository, get_ident()))
        return original_close(repository)
    monkeypatch.setattr(JobRepository, "__init__", track_init)
    monkeypatch.setattr(JobRepository, "close", track_close)
    with TestClient(create_app(config, profiles=profiles, slurm_client=fake), base_url="http://localhost") as browser:
        url = create(browser, form)
        browser.get(url)
        browser.get("/jobs")
        post(browser, url + "/submit")
        fake.states = [observation(JobState.PENDING)]
        post(browser, url + "/refresh")
    assert len(opened) == len(closed) == 6
    assert opened == closed


def test_render_validation_failure_retains_form(browser, config, form, fake):
    # Structurally valid JobSpec, rejected by existing renderer's binary check.
    form.update(run_type="compiled", executable="solver", prepare_steps=json.dumps([
        {"kind": "command", "executable": "make", "args": []},
    ]))
    response = post(browser, "/new", form)
    assert response.status_code == 400 and "solver" in response.text
    with JobRepository(config.database_path) as repository:
        assert repository.list() == []
    assert not fake.submit_calls


def test_profiles_are_frozen_at_app_creation(config, profiles, fake, form):
    app = create_app(config, profiles=profiles, slurm_client=fake)
    profiles.environments.clear()
    with TestClient(app, base_url="http://localhost") as browser:
        assert "system-python / 1" in browser.get("/new").text
        create(browser, form)


def test_no_upload_arbitrary_read_or_unbounded_form(browser, fake):
    assert browser.post("/new", files={"file": ("secret", b"x")}).status_code == 415
    assert browser.post("/new", content="x=" + "a" * MAX_FORM_BYTES,
                        headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    assert browser.post("/new", content="name=one&name=two",
                        headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert browser.get("/logs?path=/etc/passwd").status_code == 404
    assert browser.get("/static/../web.py").status_code == 404
    assert not fake.submit_calls and not fake.status_calls


def test_no_profiles_disables_creation_and_factory_config_from_env(config, fake, monkeypatch):
    monkeypatch.setenv("SBATCH_AGENT_DATABASE_PATH", str(config.database_path))
    monkeypatch.setenv("SBATCH_AGENT_RUNS_ROOT", str(config.runs_root))
    monkeypatch.delenv("SBATCH_AGENT_PROFILES_PATH", raising=False)
    with TestClient(create_app(slurm_client=fake), base_url="http://localhost") as browser:
        response = browser.get("/new")
        assert "尚未登记运行环境" in response.text and "disabled" in response.text
    assert config.database_path.exists()


def test_profiles_file_loads_registered_versions(config, profiles, fake, tmp_path):
    path = tmp_path / "profiles.yaml"
    path.write_text(profiles.model_dump_json(), encoding="utf-8")  # JSON is valid YAML
    with TestClient(create_app(replace(config, profiles_path=path), slurm_client=fake),
                    base_url="http://localhost") as browser:
        assert "system-python / 1" in browser.get("/new").text


def test_storage_and_unexpected_errors_hide_tracebacks(browser, monkeypatch):
    def failed(repository, **kwargs):
        raise PersistenceError("private database info")
    monkeypatch.setattr(JobRepository, "list", failed)
    response = browser.get("/jobs")
    assert response.status_code == 503 and "private database info" not in response.text


def test_unexpected_server_error_is_safe(config, profiles, fake, monkeypatch):
    def failed(repository, **kwargs):
        raise RuntimeError("private traceback detail")
    app = create_app(config, profiles=profiles, slurm_client=fake)
    monkeypatch.setattr(JobRepository, "list", failed)
    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as browser:
        response = browser.get("/jobs")
        assert response.status_code == 500
        assert "private traceback detail" not in response.text and "Traceback" not in response.text


def test_all_post_fields_render_and_static_styles_load(browser):
    class Fields(HTMLParser):
        def __init__(self):
            super().__init__()
            self.names = set()
        def handle_starttag(self, tag, attrs):
            if tag in {"input", "select", "textarea"}:
                self.names.add(dict(attrs).get("name"))
    fields = Fields()
    fields.feed(browser.get("/new").text)
    assert fields.names == set(DEFAULT_FORM) | {"csrf_token", "task_intent", "folder_path"}
    response = browser.get("/static/web.css")
    assert response.status_code == 200 and "text/css" in response.headers["content-type"]
