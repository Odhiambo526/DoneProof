"""Prepare -> negative -> EXTERNAL action -> positive. No executor credential input."""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doneproof.rehearsal import Rehearsal, RehearsalFailure, new_state, save, state_lock  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('step', choices=['prepare', 'negative', 'positive'])
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--pins', required=True, type=Path, help='Operator-pinned JSON map of key_id to public key')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--provider', choices=['github', 'gmail'])
    parser.add_argument('--repo')
    parser.add_argument('--issue', type=int)
    parser.add_argument('--to')
    parser.add_argument('--subject-prefix')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--allow-loopback', action='store_true')
    parser.add_argument('--repair-attempt', type=int, help='Explicit new operation identity after an expired/infrastructure attempt; never automatic')
    args = parser.parse_args()
    with state_lock(args.state):
        run(args)


def run(args):
    if args.state.exists():
        state = json.loads(args.state.read_text())
    else:
        if args.step != 'prepare':
            raise RehearsalFailure('prepare_state_first')
        state = new_state(args.provider, repo=args.repo, number=args.issue, to=args.to, subject=args.subject_prefix)
        save(args.state, state)  # Persist idempotency identity BEFORE any network mutation.
    if args.repair_attempt is not None:
        if args.step != 'positive' or state['stage'] != 'NEGATIVE' or not 1 <= args.repair_attempt <= 100:
            raise RehearsalFailure('invalid_repair_attempt')
        state['repair_attempt'] = args.repair_attempt
        save(args.state, state)
    runner = Rehearsal(args.base_url, os.environ['DONEPROOF_API_KEY'], json.loads(args.pins.read_text()),
                       allow_loopback=args.allow_loopback)
    try:
        if args.step == 'prepare':
            runner.prepare(state)
        else:
            getattr(runner, args.step)(state, args.timeout)
        save(args.state, state)
        print(json.dumps({'stage': state['stage'], 'session_id': state['session_id'],
                          'target': state['target'], 'receipts': state['receipts'],
                          'external_action_performed_by_doneproof': False}))
    finally:
        runner.close()


if __name__ == '__main__':
    try:
        main()
    except RehearsalFailure as exc:
        print(json.dumps({'passed': False, 'code': str(exc)}))
        sys.exit(1)
    except Exception:
        print(json.dumps({'passed': False, 'code': 'rehearsal_unavailable'}))
        sys.exit(1)
