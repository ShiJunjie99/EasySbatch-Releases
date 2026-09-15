"""Mode semantics and the minimized real 2026-09-08 13:43 DeepSeek failure.

The invalid response must still fail. Fix the producing contract, not the guard.
All I/O is the existing Scanner + fake provider transport; no real model calls.
"""
import json
from pathlib import Path

import pytest

from sbatch_agent.analysis_context import AnalysisContextBuilder, OUTPUT_TEMPLATE, SYSTEM_INSTRUCTION
from sbatch_agent.analysis_models import DraftFields, PARALLELISM_RULES, StructuredAnalysis
from sbatch_agent.analyzer import AIProjectAnalyzer, structured_output_schema
from sbatch_agent.model_client import AnalysisOutputValidationError, OpenAICompatibleClient
from sbatch_agent.scanner import ProjectScanner
from test_analyzer import analyze, proposed, ref, offline
from test_model_client import config, envelope, transport

FIXTURE = Path(__file__).parent / 'fixtures/analysis/deepseek_parallelism_missing_refs.json'


def scan(tmp_path, readme='python hello.py\n', shell=None):
    (tmp_path / 'README.md').write_text(readme)
    (tmp_path / 'hello.py').write_text('print("hello world")\n')
    if shell:
        (tmp_path / 'old.sbatch').write_text(shell)
    return ProjectScanner().scan(tmp_path)


def test_real_failure_preserved_through_provider_and_schema(monkeypatch, tmp_path):
    data = json.loads(FIXTURE.read_text())
    monkeypatch.setenv('TEST_MODEL_CREDENTIAL', 'offline-placeholder')
    calls = transport(monkeypatch, envelope(content=json.dumps(data)))
    evidence = scan(tmp_path)
    client = OpenAICompatibleClient(config(provider='deepseek'))
    # Provider JSON parsing neither invents refs nor coerces false to unknown.
    context = AnalysisContextBuilder().build(evidence, 'hello world')
    assert client.generate_structured(context=context, schema=structured_output_schema()).data == data
    with pytest.raises(AnalysisOutputValidationError) as caught:
        AIProjectAnalyzer(model_client=client).analyze(evidence=evidence, task_intent='hello world')
    assert caught.value.safe_diagnostic() == 'stage=schema field=parallelism.threads reason=inconsistent_proposal'
    assert len(calls) == 2  # Two explicit calls above, zero implicit retries.


def test_unknown_template_is_consistent_and_passes_post_validation(tmp_path):
    evidence = scan(tmp_path)
    output = json.loads(OUTPUT_TEMPLATE)
    result = analyze(evidence, output)
    assert not evidence.parallelism_hints
    for mode in result.draft.parallelism.model_dump().values():
        assert mode['value'] is None and mode['status'] == 'UNRESOLVED'
    assert result.draft.resource_requirements.cpus_per_task.value is None


def test_schema_prompt_share_boolean_contract():
    schema = structured_output_schema()
    parallelism = schema['$defs']['Parallelism']
    assert parallelism['description'] == PARALLELISM_RULES
    assert PARALLELISM_RULES in SYSTEM_INSTRUCTION
    boolean_ref = parallelism['properties']['threads']['$ref'].split('/')[-1]
    assert schema['$defs'][boolean_ref]['properties']['value']['anyOf'] == [{'type': 'boolean'}, {'type': 'null'}]
    assert 'false is a claim' in schema['$defs'][boolean_ref]['properties']['value']['description']
    assert StructuredAnalysis.model_validate_json(OUTPUT_TEMPLATE).draft == DraftFields()


@pytest.mark.parametrize('mode,text,kind', [
    ('threads', 'This program uses OpenMP.\n', 'threads'),
    ('threads', 'OpenMP configuration: OMP_NUM_THREADS=8\n', 'threads'),
    ('mpi', 'mpirun -np 4 python hello.py\n', 'mpi'),
    ('gpu', 'Optional CUDA GPU support.\n', 'gpu'),
])
def test_positive_hints_are_inferred_never_allocation_counts(tmp_path, mode, text, kind):
    evidence = scan(tmp_path, 'python hello.py\n' + text)
    result = analyze(evidence, {'draft': {'parallelism': {mode: proposed(True, ref(evidence, kind))}}})
    field = getattr(result.draft.parallelism, mode)
    assert field.value is True and field.status == 'INFERRED'
    assert all(p['status'] == 'UNRESOLVED' for p in result.draft.resource_requirements.model_dump().values())


def test_explicit_serial_remains_conservative_unknown(tmp_path):
    evidence = scan(tmp_path, 'Runs in serial mode.\npython hello.py\n')
    result = analyze(evidence, {'draft': {'parallelism': {'serial': proposed(True, ref(evidence, 'readme'))}}})
    assert result.draft.parallelism.serial.status == 'UNRESOLVED'
    assert result.draft.parallelism.serial.value is None


@pytest.mark.parametrize('mode', ['serial', 'threads', 'mpi', 'gpu'])
def test_absence_cannot_become_negative_or_serial_claim(tmp_path, mode):
    evidence = scan(tmp_path)
    result = analyze(evidence, {'draft': {'parallelism': {mode: proposed(mode == 'serial', ref(evidence, 'readme'), status='INFERRED')}}})
    assert getattr(result.draft.parallelism, mode).status == 'UNRESOLVED'


def test_serial_readme_conflicts_with_existing_mpirun(tmp_path):
    evidence = scan(tmp_path, 'Runs in serial mode.\npython hello.py\n', 'mpirun -np 4 python hello.py\n')
    refs = ref(evidence, 'readme') + ref(evidence, 'mpi')
    result = analyze(evidence, {'draft': {'parallelism': {'mpi': proposed(True, refs)}},
        'conflicts': [{'field': 'parallelism.mpi', 'reason': 'README serial conflicts with historical MPI command.', 'evidence_refs': refs}]})
    assert result.draft.parallelism.mpi.value is None and result.draft.parallelism.mpi.status == 'UNRESOLVED'
    assert len(result.conflicts) == 1


@pytest.mark.parametrize('value', [1, 8, '8', 'false', {}, []])
def test_threads_illegal_type_rejected_even_with_refs(tmp_path, value):
    evidence = scan(tmp_path)
    with pytest.raises(AnalysisOutputValidationError) as caught:
        analyze(evidence, {'draft': {'parallelism': {'threads': proposed(value, ref(evidence, 'readme'))}}})
    assert caught.value.safe_diagnostic() == 'stage=schema field=parallelism.threads reason=schema_mismatch'


def test_unsupported_thread_quantity_does_not_become_cpu_allocation(tmp_path):
    evidence = scan(tmp_path, 'python hello.py\nOpenMP\n')
    result = analyze(evidence, {'draft': {'parallelism': {'threads': proposed(True, ref(evidence, 'threads'))},
        'resource_requirements': {'cpus_per_task': proposed(8, ref(evidence, 'threads'))}}})
    assert result.draft.parallelism.threads.status == 'INFERRED'
    assert result.draft.resource_requirements.cpus_per_task.value is None
    assert result.draft.resource_requirements.cpus_per_task.status == 'UNRESOLVED'


@pytest.mark.parametrize('value,status,refs', [(False, 'INFERRED', []), (True, 'DIRECT', []), (False, 'UNRESOLVED', []), (None, 'DIRECT', [])])
def test_inconsistent_boolean_proposals_still_block(tmp_path, value, status, refs):
    with pytest.raises(AnalysisOutputValidationError) as caught:
        analyze(scan(tmp_path), {'draft': {'parallelism': {'threads': proposed(value, refs, status=status)}}})
    assert caught.value.reason == 'inconsistent_proposal'
