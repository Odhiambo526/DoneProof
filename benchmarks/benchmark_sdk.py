"""Offline, reproducible SDK overhead and session composition measurements."""
import argparse
import asyncio
import gc
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'examples'))

import httpx  # noqa: E402
from fixture_runtime import fixture  # noqa: E402

from doneproof import AsyncDoneProof  # noqa: E402
from doneproof.assurance_models import AssuranceSession  # noqa: E402


async def benchmark(directory):
    app, worker, provider = fixture(Path(directory) / 'benchmark.db')
    transport = httpx.ASGITransport(app)
    samples, end_to_end, polls = [], [], []
    calls = []
    async def request(req):
        calls.append(req.url.path)
        return await transport.handle_async_request(req)
    async with AsyncDoneProof(api_key='fixture-key', base_url='https://fixture.invalid', transport=httpx.MockTransport(request)) as dp:
        for n in range(10):
            provider.state = 'open'
            start = time.perf_counter()
            session = await dp.assurance.prepare(task='Close issue #12 in acme/api', idempotency_key='benchmark-' + str(n))
            samples.append((time.perf_counter() - start) * 1000)
            provider.state = 'closed'
            async def run_worker():
                while True:
                    await worker.tick()
                    await asyncio.sleep(0.01)
            task = asyncio.create_task(run_worker())
            before = len(calls)
            start = time.perf_counter()
            try:
                result = await dp.assurance.verify(session.id, wait=True)
                assert result.verdict == 'VERIFIED'
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            end_to_end.append((time.perf_counter() - start) * 1000)
            polls.append(sum(path.startswith('/v1/jobs/') for path in calls[before:]))
        before = len(calls)
        await dp.providers.declarations()
        await dp.providers.declarations()
        cached = len(calls) - before
        before = len(calls)
        await dp.providers.available()
        await dp.providers.available()
        live = len(calls) - before
    # Both language benchmarks use the identical ready-session compatibility vector.
    vector = json.loads((ROOT / 'sdk/typescript/test/fixtures.json').read_text())['ready_session']
    raw = json.dumps(vector, separators=(',', ':'), ensure_ascii=False)
    encoded = raw.encode()
    serialization = []
    for _ in range(1000):
        start = time.perf_counter()
        AssuranceSession.model_validate_json(raw).model_dump_json()
        serialization.append((time.perf_counter() - start) * 1000)
    async def reply(req):
        return httpx.Response(200, content=encoded)
    overhead = []
    async with AsyncDoneProof(api_key='fixture-key', base_url='https://fixture.invalid', transport=httpx.MockTransport(reply)) as dp:
        for _ in range(100):
            start = time.perf_counter()
            await dp.assurance.get(vector['id'])
            overhead.append((time.perf_counter() - start) * 1000)
    return {'fixture': True, 'storage': 'sqlite', 'network': 'in-process ASGI; provider is offline fixture',
        'prepare_session_ms_mean': statistics.mean(samples), 'prepare_session_ms_max': max(samples),
        'end_to_end_verification_ms_mean': statistics.mean(end_to_end),
        'polling_requests_per_job': polls, 'provider_catalog_requests_for_two_calls': cached,
        'live_capability_requests_for_two_calls': live,
        'python_serialize_parse_ms_mean': statistics.mean(serialization),
        'python_sdk_no_network_ms_mean': statistics.mean(overhead), 'session_bytes': len(encoded)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='benchmarks/results/sdk-python.json')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='doneproof-benchmark-') as directory:
        result = asyncio.run(benchmark(directory))
        gc.collect()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result))
