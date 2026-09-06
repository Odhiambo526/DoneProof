"""Run: python examples/assured_agent.py (after pip install -e .). No external accounts."""
import asyncio
import gc
import tempfile
from pathlib import Path

import httpx
from fixture_runtime import fixture

from doneproof import AsyncDoneProof


async def main(directory):
    print('OFFLINE DEMO FIXTURE: local issue state, local test key; not real GitHub evidence.')
    app, worker, external_issue = fixture(Path(directory) / 'demo.db')
    worker_task = asyncio.create_task(worker.run())
    try:
        async with AsyncDoneProof(api_key='fixture-key', base_url='https://fixture.invalid',
                                  transport=httpx.ASGITransport(app)) as dp:
            session = await dp.assurance.prepare(task='Close issue #12 in acme/api', idempotency_key='invoice-1842')
            assert session.state == 'READY_FOR_EXECUTION'
            print('Prepared trusted boundary. External fixture agent claims success (state remains open).')
            result = await dp.assurance.verify(session.id, wait=True)
            assert result.verdict == 'FAILED'
            print('Independent result:', result.verdict.value)
            for issue in result.remediation:
                print(issue.action_hint)
            external_issue.state = 'closed'
            print('External fixture agent repairs its issue state.')
            repaired = await dp.assurance.reverify(session.id, previous_receipt_id=result.receipt.receipt_id,
                                                   idempotency_key='invoice-1842-repair-1', wait=True)
            assert repaired.verdict == 'VERIFIED' and len(repaired.lineage) == 2
            assert dp.verify_receipt(repaired.receipt, app.state.signer.public_key_b64)
            print('VERIFIED; immutable two-receipt chain; pinned Ed25519 verification passed.')
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='doneproof-demo-') as directory:
        asyncio.run(main(directory))
        gc.collect()
