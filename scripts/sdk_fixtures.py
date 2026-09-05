"""Deterministic, offline receipt compatibility vectors shared by both SDKs."""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'examples'))

from fixture_runtime import fixture_settings  # noqa: E402

from doneproof.assurance_models import AssuranceSession  # noqa: E402
from doneproof.browser_models import BrowserProvenance  # noqa: E402
from doneproof.compilation_models import CompilationResult, ContractQuality  # noqa: E402
from doneproof.domain import CompletionContract, VerificationReceipt  # noqa: E402
from doneproof.recovery_models import RecoveryInfo  # noqa: E402
from doneproof.remediation import remediation_for  # noqa: E402
from doneproof.signing import ReceiptSigner  # noqa: E402


def vectors():
    legacy = json.loads((ROOT / 'tests/fixtures/legacy_recovery_receipt.json').read_text())
    old = VerificationReceipt.model_validate(legacy['receipt'])
    signer = ReceiptSigner(fixture_settings('unused'))
    middle = old.model_copy(deep=True)
    middle.schema_version = '1.1'
    middle.recovery = RecoveryInfo(chain_id=middle.receipt_id)
    middle.remediation = remediation_for(middle.results)
    signer.sign(middle)
    browser = middle.model_copy(deep=True)
    browser.schema_version = '1.2'
    browser.results[0].evidence.provider = 'browser'
    browser.results[0].evidence.provenance = BrowserProvenance(outcome='recognized', fresh_context=True)
    signer.sign(browser)
    receipts = [{'receipt': r.model_dump(mode='json'), 'pinned_public_key': r.public_key,
                 'signed_payload_b64': base64.b64encode(ReceiptSigner._payload(r)).decode()} for r in [old, middle, browser]]
    contract = CompletionContract.model_validate(legacy['contract'])
    ready = AssuranceSession(id='as_' + 'a' * 32, workspace='tenant-a', task=contract.task,
        state='READY_FOR_EXECUTION', compiler=CompilationResult(status='valid_contract', contract=contract,
        contract_quality=ContractQuality(confidence=0.9)), contract=contract, trusted_task_started_at=contract.task_started_at,
        created_at=contract.created_at, updated_at=contract.created_at)
    return {'receipts': receipts, 'ready_session': ready.model_dump(mode='json'), 'fixture': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = ROOT / 'sdk/typescript/test/fixtures.json'
    content = json.dumps(vectors(), sort_keys=True, indent=2) + '\n'
    if args.check:
        if target.read_text(encoding='utf-8') != content:
            raise SystemExit('SDK receipt fixtures are stale')
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8', newline='\n')
