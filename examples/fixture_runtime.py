"""Offline demonstration fixture. Never import this module into production routing.

The fixture replaces GitHub network observation with an independently read state.
It does not represent real GitHub evidence or a production assurance deployment.
"""
import asyncio
import base64
import gc
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doneproof.adapters.base import ProviderAdapter, ProviderObservation  # noqa: E402
from doneproof.app import create_app  # noqa: E402
from doneproof.config import Settings  # noqa: E402
from doneproof.worker import VerificationWorker  # noqa: E402


class FixtureIssue(ProviderAdapter):
    def __init__(self):
        self.state = 'open'

    async def observe(self, selector, context):
        return ProviderObservation({'number': 12, 'state': self.state}, note='OFFLINE DEMO FIXTURE; not real GitHub evidence')


def fixture_settings(path):
    return Settings(env='test', db_path=str(path), api_keys={'fixture-key': 'fixture-workspace'},
        cors_origins=(), verification_timeout_seconds=2, openai_api_key=None, openai_model='gpt-6-astra',
        github_token=None, gmail_tokens={}, gmail_access_token=None, webhook_sources={}, webhook_max_skew_seconds=600,
        signing_seed_b64=base64.b64encode(b'D' * 32).decode(), legacy_receipt_key=None,
        max_body_bytes=1048576, requests_per_minute=10000, max_batch_size=25)


def fixture(path):
    provider = FixtureIssue()
    app = create_app(fixture_settings(path), {'github': provider})
    return app, VerificationWorker(app.state.store, app.state.engine), provider


async def serve(directory):
    import uvicorn
    app, worker, provider = fixture(Path(directory) / 'fixture.db')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False, lifespan='off'))
    worker_task = asyncio.create_task(worker.run())
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    print(json.dumps({'baseUrl': 'http://127.0.0.1:' + str(sock.getsockname()[1]),
                      'pinnedPublicKey': app.state.signer.public_key_b64, 'fixture': True}), flush=True)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            if line.strip() == 'repair':
                # Controlled fixture state changed by the external demonstration agent.
                provider.state = 'closed'
                print(json.dumps({'fixture_repaired': True}), flush=True)
            elif line.strip() == 'stop':
                break
    finally:
        server.should_exit = True
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await server_task


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='doneproof-fixture-') as directory:
        asyncio.run(serve(directory))
        gc.collect()
