"""UI-format boundaries preserve the existing executable argument/seconds model."""

import pytest
from fastapi.testclient import TestClient

from sbatch_agent.ui_formatting import format_args, parse_args, format_duration, parse_duration
from sbatch_agent.web_forms import spec_from_form
from sbatch_agent.presentation import status_badge
from test_smart_web import web, prepare, act
from test_web import form, profiles


@pytest.mark.parametrize('seconds,text', [(1,'00:00:01'), (120,'00:02:00'), (7200,'02:00:00'),
                                         (90061,'25:01:01'), (172800,'48:00:00')])
def test_duration_roundtrip_and_legacy_seconds(seconds,text):
    assert format_duration(seconds)==text
    assert parse_duration(text)==parse_duration(str(seconds))==seconds


@pytest.mark.parametrize('text',['2h','02:60:00','02:00:60','-1:00:00','1:20',' 02:00:00',''])
def test_invalid_duration_is_rejected(text):
    with pytest.raises(ValueError,match='运行时限'):
        parse_duration(text)


@pytest.mark.parametrize('args', [[], ['--input','case 01.json'], ['','双引号"单引号\''],
    ['$HOME','$(touch /tmp/must-not-exist)',';','a\nb','*.dat','[literal]','{}']])
def test_arguments_roundtrip_without_interpretation(args):
    assert parse_args(format_args(args))==args


def test_legacy_json_arguments_still_supported():
    assert parse_args('["--input", "case 01.json", ""]')==['--input','case 01.json','']


@pytest.mark.parametrize('value',['--input "unclosed','[1]','{}','["\\ud800"]','["\\u0000"]'])
def test_invalid_arguments_do_not_reach_domain(value):
    with pytest.raises(ValueError,match='程序参数'):
        parse_args(value)


def test_manual_ui_formats_preserve_jobspec(form,profiles):
    form.update(time_limit_seconds='02:00:00',args='--input "case 01.json" "$HOME"')
    spec=spec_from_form(form,profiles)
    assert spec.resources.time_limit_seconds==7200
    assert spec.run_step.args==['--input','case 01.json','$HOME']


def test_smart_continue_uses_same_duration_and_args_contract(tmp_path):
    app,config,root,model,cluster,fake=web(tmp_path,omit=('memory_mib','time_limit_seconds'))
    (root/'inputs/case 01.json').write_text('{}')
    with TestClient(app,base_url='http://localhost') as browser:
        url=prepare(browser,root)
        response=act(browser,url,'continue',{'memory_mib':'256','time_limit_seconds':'00:02:00',
                                           'args':'run.py --input "inputs/case 01.json"'})
        assert response.status_code==303
        entry=app.state.prepared_store.entries[url.rsplit('/',1)[1]]
        assert entry.prepared.job_spec.resources.time_limit_seconds==120
        assert entry.prepared.job_spec.run_step.args==['run.py','--input','inputs/case 01.json']
        page=browser.get(url)
        assert '00:02:00' in page.text and 'HH:MM:SS' in page.text
        assert not fake.submit_calls and len(model.calls)==cluster.calls==1


def test_flagged_node_state_remains_meaningful():
    assert status_badge('IDLE+DRAIN').label=='停止分配'
    assert status_badge('IDLE+DRAIN').description=='IDLE+DRAIN'
    assert status_badge('FAILED').symbol=='×'
