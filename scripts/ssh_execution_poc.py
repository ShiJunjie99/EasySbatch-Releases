"""M10-A development CLI. Never start this from a Web request."""
import argparse
import json
import logging
from sbatch_agent.ssh_poc import SSHExecutionTarget, SSHExecutionContext, Operation, SSHProbeError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Trusted deployment TOML, no credentials.')
    parser.add_argument('--operation', choices=[v.value for v in Operation], default='identity')
    parser.add_argument('--experiment-id', required=True, help='One UUID for this authorized experiment.')
    parser.add_argument('--interactive', action='store_true', help='Native OpenSSH terminal authentication, no password option.')
    parser.add_argument('--authorize-one-smoke', action='store_true', help='Explicitly allow at most one tiny PoC job for this user.')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    try:
        target = SSHExecutionTarget.load(args.config)
        context = SSHExecutionContext(target, interactive=args.interactive)
        report = context.run(Operation(args.operation), experiment=args.experiment_id,
                             authorize_smoke=args.authorize_one_smoke)
        print(json.dumps(report, indent=2))
        return 0
    except SSHProbeError as exc:
        print(json.dumps({'result':'FAIL', 'error_code':exc.code, 'automatic_retry':False,
                          'evidence':exc.evidence}))
        return 2
    except ValueError:
        print(json.dumps({'result':'FAIL', 'error_code':'POC_INPUT_INVALID'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
