"""Manual, billable provider check; never collected or run automatically by pytest.

Without --project-dir: one tiny structured API call, no project scan.
With --project-dir: the same check, then one Analyzer call only if it succeeds.
No execution chain, writes, persisted analysis or networking changes. The tiny
probe is single-attempt; optional Analyzer uses the bounded transport policy.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from sbatch_agent.analysis_context import AnalysisContext, AnalysisInputError
from sbatch_agent.analysis_models import field_proposals
from sbatch_agent.analyzer import AIProjectAnalyzer
from sbatch_agent.model_client import (
    AnalysisOutputValidationError, ModelErrorCode, ModelUnavailableError,
)
from sbatch_agent.model_factory import model_client_from_env
from sbatch_agent.profiles import StaticProfiles
from sbatch_agent.scanner import ProjectScanner, ProjectScanError


PROBE_SCHEMA = {
    "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"], "additionalProperties": False,
}


def provider_probe(client):
    """No project data: exercise the existing transport with a tiny schema."""
    response = client.generate_structured(
        context=AnalysisContext(
            "Return only the requested structured JSON. You have no tools. Do not execute anything.",
            '{"request":"Return status ok for this connectivity check."}', (), (),
        ), schema=PROBE_SCHEMA,
    )
    if response.data != {"status": "ok"}:
        raise AnalysisOutputValidationError("Provider probe does not match its schema.")
    return {"structured_output": "passed", "request_id": response.request_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", help="Optional small project root; sends selected evidence only after the provider probe passes.")
    parser.add_argument("--task-intent", help="Task description for the optional Analyzer check.")
    parser.add_argument("--profiles", type=Path, help="Optional existing registered profiles YAML/JSON for Analyzer.")
    args = parser.parse_args(argv)
    if bool(args.project_dir) != bool(args.task_intent) or (args.profiles and not args.project_dir):
        parser.error("--project-dir and --task-intent must be supplied together; --profiles requires them.")

    report = {"captured_at": datetime.now(timezone.utc).isoformat(), "phase": "configuration"}
    try:
        try:
            client = model_client_from_env()
        except (ValueError, TypeError):
            raise ModelUnavailableError(code=ModelErrorCode.INVALID_CONFIG) from None
        if client is None:
            raise ModelUnavailableError(code=ModelErrorCode.NOT_CONFIGURED)
        if client.availability().state != "available":
            raise ModelUnavailableError(code=ModelErrorCode.CREDENTIAL_MISSING)
        report.update(provider=client.provider, model=client.model, phase="provider")
        report["provider_probe"] = provider_probe(client)
        if args.project_dir:
            report["phase"] = "analyzer"
            profiles = StaticProfiles()
            if args.profiles:
                # Trusted local configuration only; not saved or sent to the model.
                import yaml
                profiles = StaticProfiles.model_validate(yaml.safe_load(args.profiles.read_text(encoding="utf-8")))
            evidence = ProjectScanner().scan(args.project_dir)
            result = AIProjectAnalyzer(model_client=client, profiles=profiles).analyze(
                evidence=evidence, task_intent=args.task_intent,
            )
            # No root, source snippets, raw provider body, or credential in report.
            report["analysis"] = {
                "post_validation": "passed", "manual_review_required": True,
                "project_types": [c.value for c in evidence.project_type_candidates],
                "fields": {name: {"value": proposal.model_dump(mode="json")["value"],
                                  "status": proposal.status, "evidence_refs": proposal.evidence_refs}
                           for name, proposal in field_proposals(result.draft)},
                "environment_resolution": result.draft.environment_resolution.model_dump(mode="json"),
                "unresolved": [u.model_dump() for u in result.draft.unresolved],
                "warnings": result.warnings,
                "conflicts": [{"field": c.field, "evidence_refs": c.evidence_refs} for c in result.conflicts],
                "request_id": result.model_metadata.request_id,
            }
        report["outcome"] = "passed"
    except ModelUnavailableError as exc:
        report.update(outcome="unavailable", error_code=exc.code.value, http_status=exc.http_status)
    except AnalysisOutputValidationError as exc:
        report.update(outcome="failed", error_code="invalid_structured_response", diagnostic=exc.safe_diagnostic())
    except (ProjectScanError, AnalysisInputError):
        report.update(outcome="failed", error_code="invalid_project_or_intent")
    except Exception:
        # Never dump provider exceptions, request headers, input paths or raw text.
        report.update(outcome="failed", error_code="smoke_configuration_or_client_error")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["outcome"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
