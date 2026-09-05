import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest

from doneproof import AsyncDoneProof, DoneProof
from doneproof.assurance_models import CompletionEvent
from doneproof.sdk_callbacks import MemoryReplayStore, verify_callback
from doneproof.sdk_common import (
    CallbackError,
    Cancellation,
    DoneProofError,
    DoneProofTimeout,
    DuplicateEvent,
    VerificationCancelled,
)
from tests.test_assurance import TASK, assurance  # noqa: F401


def test_sync_and_async_clients_share_api_semantics(assurance):  # noqa: F811
    app, _, worker, provider = assurance
    async def workflow(dp):
        session = await dp.assurance.prepare(**TASK, idempotency_key='sdk-outcome')
        assert not session.needs_clarification
        initial = await dp.assurance.verify(session.id)
        await worker.run_until_terminal('tenant-a', initial.current_job_id)
        failed = await dp.assurance.wait_for_verification(session.id)
        assert failed.verdict == 'FAILED'
        provider.state = 'closed'
        repair = await dp.assurance.reverify(session.id, previous_receipt_id=failed.receipt.receipt_id, idempotency_key='repair')
        await worker.run_until_terminal('tenant-a', repair.current_job_id)
        return session.id
    async def run():
        async with AsyncDoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.ASGITransport(app)) as dp:
            return await workflow(dp)
    identifier = asyncio.run(run())
    with DoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.ASGITransport(app)) as dp:
        result = dp.assurance.wait_for_verification(identifier)
        assert result.verdict == 'VERIFIED'
        assert dp.verify_receipt(result.receipt, app.state.signer.public_key_b64)
        assert dp.providers.available().version


@pytest.mark.parametrize('status', [429, 500, 503, None])
def test_transient_retry_preserves_body_key_and_request_id(assurance, status):  # noqa: F811
    app, _, _, _ = assurance
    seen = []
    transport = httpx.ASGITransport(app)
    async def handler(request):
        seen.append(request)
        if len(seen) == 1:
            if status is None:
                # Server committed preparation but the HTTP response was lost.
                await transport.handle_async_request(request)
                raise httpx.ReadError('secret-sentinel', request=request)
            return httpx.Response(status, headers={'Retry-After': '0'})
        return await transport.handle_async_request(request)
    with DoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.MockTransport(handler)) as dp:
        result = dp.assurance.prepare(**TASK, idempotency_key='stable-operation')
    assert result.state == 'READY_FOR_EXECUTION' and len(seen) == 2
    assert seen[0].content == seen[1].content
    assert seen[0].headers['Idempotency-Key'] == seen[1].headers['Idempotency-Key']
    assert seen[0].headers['X-Request-ID'] == seen[1].headers['X-Request-ID']


@pytest.mark.parametrize('status', [400, 401, 403, 409, 422])
def test_semantic_http_errors_not_retried_or_leaked(status):
    calls, logs = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={'detail': 'private-body-secret-sentinel'})
    with DoneProof(api_key='api-key-sentinel', base_url='https://testserver', transport=httpx.MockTransport(handler), log_hook=logs.append) as dp:
        with pytest.raises(DoneProofError) as error:
            dp.assurance.prepare(**TASK, idempotency_key='safe')
    assert len(calls) == 1 and len(logs) == 1
    assert 'sentinel' not in str(error.value) + repr(logs)
    assert not hasattr(error.value, 'request') and not hasattr(error.value, 'response')


def test_absolute_deadline_and_retry_after():
    calls = []
    async def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '100'})
    with DoneProof(api_key='k', base_url='https://testserver', transport=httpx.MockTransport(handler)) as dp:
        with pytest.raises(DoneProofTimeout):
            dp.assurance.prepare(**TASK, idempotency_key='safe', timeout=0.1)
    assert len(calls) == 1
    async def slow(request):
        await asyncio.sleep(5)
    started = time.monotonic()
    with DoneProof(api_key='k', base_url='https://testserver', transport=httpx.MockTransport(slow)) as dp:
        with pytest.raises(DoneProofTimeout):
            dp.assurance.prepare(**TASK, idempotency_key='safe', timeout=0.05)
    assert time.monotonic() - started < 1


def test_local_cancel_does_not_submit_or_mutate_server():
    cancel = Cancellation()
    cancel.cancel()
    def forbidden(request):
        raise AssertionError('No request should be sent')
    with DoneProof(api_key='k', base_url='https://testserver', transport=httpx.MockTransport(forbidden)) as dp:
        with pytest.raises(VerificationCancelled):
            dp.assurance.verify('as_123', wait=True, cancellation=cancel)


def signed_event():
    now = int(time.time())
    event = CompletionEvent(event_id='ve_' + 'a' * 32, job_id='vj_' + 'a' * 32,
                            state='COMPLETE', receipt_id='vr_' + 'a' * 32, finished_at=now)
    body = event.model_dump_json().encode()
    signature = hmac.new(b's' * 32, str(now).encode() + b'.' + body, hashlib.sha256).hexdigest()
    return body, {'X-DoneProof-Event': event.event_id, 'X-DoneProof-Timestamp': str(now),
                  'X-DoneProof-Signature': 'sha256=' + signature}


def test_callback_signature_timestamp_deduplication_and_event_binding():
    body, headers = signed_event()
    store = MemoryReplayStore()
    event = verify_callback(body, headers, secret='s' * 32, replay_store=store)
    assert event.state == 'COMPLETE'
    with pytest.raises(DuplicateEvent):
        verify_callback(body, headers, secret='s' * 32, replay_store=store)
    for changed_body, changed_headers in [(body + b' ', headers),
            (body, {**headers, 'X-DoneProof-Event': 'other'}),
            (body, {**headers, 'X-DoneProof-Timestamp': '0'}),
            (json.dumps({'secret': 'sentinel'}).encode(), headers)]:
        with pytest.raises(CallbackError) as error:
            verify_callback(changed_body, changed_headers, secret='s' * 32, replay_store=MemoryReplayStore())
        assert 'sentinel' not in str(error.value)


def test_actual_worker_callback_is_accepted_by_sdk(assurance):  # noqa: F811
    from doneproof.job_callbacks import CallbackRegistry
    app, _, worker, _ = assurance
    app.state.job_callbacks = CallbackRegistry({'tenant-a': {'complete': {
        'url': 'https://receiver.example.org/completion', 'secret': 's' * 32}}})
    worker.callbacks = app.state.job_callbacks
    events = []
    def receiver(request):
        events.append(verify_callback(request.content, request.headers, secret='s' * 32, replay_store=MemoryReplayStore()))
        return httpx.Response(204)
    worker.callback_transport = httpx.MockTransport(receiver)
    async def run():
        async with AsyncDoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.ASGITransport(app)) as dp:
            prepared = await dp.assurance.prepare(**TASK, idempotency_key='callback-session')
            submitted = await dp.assurance.verify(prepared.id, callback_id='complete')
            await worker.run_until_terminal('tenant-a', submitted.current_job_id)
            assert await worker.callback_tick()
            result = await dp.get_job(submitted.current_job_id)
            assert result.callback.state == 'DELIVERED' and result.callback.attempts == 1
            assert events[0].job_id == result.id and events[0].receipt_id == result.receipt_id
    asyncio.run(run())


def test_lost_verify_response_creates_one_job(assurance):  # noqa: F811
    app, _, _, _ = assurance
    transport = httpx.ASGITransport(app)
    lost = False
    async def handler(request):
        nonlocal lost
        response = await transport.handle_async_request(request)
        if request.url.path.endswith('/verify') and not lost:
            lost = True
            await response.aclose()
            raise httpx.ReadError('private-provider-sentinel')
        return response
    with DoneProof(api_key='key-a', base_url='https://testserver', transport=httpx.MockTransport(handler)) as dp:
        session = dp.assurance.prepare(**TASK, idempotency_key='lost-response')
        result = dp.assurance.verify(session.id)
        assert result.current_job_id
    db = app.state.jobs
    with db.transaction() as con:
        assert db.execute(con, 'SELECT COUNT(*) AS n FROM verification_jobs').fetchone()['n'] == 1


def test_cancel_interrupts_inflight_network_request():
    cancel = Cancellation()
    async def slow(request):
        await asyncio.sleep(10)
    async def run():
        async with AsyncDoneProof(api_key='k', base_url='https://testserver', transport=httpx.MockTransport(slow)) as dp:
            task = asyncio.create_task(dp.assurance.verify('as_123', cancellation=cancel))
            await asyncio.sleep(0.02)
            cancel.cancel()
            with pytest.raises(VerificationCancelled):
                await asyncio.wait_for(task, 1)
    asyncio.run(run())
