"""M10-A2 CLI. Authorized operators authenticate in native hidden SSH prompts."""
import argparse
import json
import logging
import resource
import sys
from uuid import uuid4
from sbatch_agent.ssh_poc import SSHExecutionTarget, OpenSSHTransport, ProbeProgram
from sbatch_agent.ssh_poc_deployment import SSH_HOST, SSH_PORT
from sbatch_agent.ssh_poc_two_user import TwoUserContext, TwoUserExperiment
from sbatch_agent.ssh_poc_session import SSHExperimentTransport


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo an accidentally pasted sensitive argument.
        self.exit(2, 'POC_INPUT_INVALID; use --help for supported arguments.\n')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--user-a', default='alice')
    parser.add_argument('--uid-a', required=True, type=int)
    parser.add_argument('--user-b', required=True)
    parser.add_argument('--uid-b', required=True, type=int)
    parser.add_argument('--known-hosts', help='Already verified public host keys; no auto acceptance.')
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument('--interactive', action='store_true', help='Native OpenSSH prompts; existing agent authentication is also allowed.')
    authentication.add_argument('--password-prompt', action='store_true', help='Native hidden password input once per user. Two independent SSH connections, closed at experiment end; no password cache.')
    parser.add_argument('--identity-only', action='store_true')
    parser.add_argument('--authorize-two-smokes', action='store_true')
    args = parser.parse_args(argv)
    interactive = args.interactive or args.password_prompt
    if interactive and not sys.stdin.isatty():
        print(json.dumps({'overall':'PARTIAL','error_code':'AUTHENTICATION_FLOW_BLOCKED'}))
        return 2
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    try:
        experiment = str(uuid4())
        contexts = []
        for user, uid in ((args.user_a,args.uid_a),(args.user_b,args.uid_b)):
            target = SSHExecutionTarget(SSH_HOST,SSH_PORT,user,uid,args.known_hosts)
            transport = (SSHExperimentTransport(target, experiment) if args.password_prompt
                         else OpenSSHTransport(program=ProbeProgram.TWO_USER))
            contexts.append(TwoUserContext(target, experiment=experiment,
                                           interactive=interactive, transport=transport))
        report = TwoUserExperiment(*contexts).run(authorize_two_smokes=args.authorize_two_smokes,
                                                  identity_only=args.identity_only)
        report['authentication_method'] = ('native-terminal-password' if args.password_prompt
                                           else 'native-interactive' if interactive else 'ssh-agent')
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return 0 if report['overall']=='PASS' else 2
    except ValueError:
        print(json.dumps({'overall':'FAIL','error_code':'POC_INPUT_INVALID'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
