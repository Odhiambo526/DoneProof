import json
import httpx

from doneproof.client import DoneProofClient


def test_python_client_sets_auth_and_idempotency():
    seen=[]
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == '/v1/runs/cc_1/verify':
            return httpx.Response(200,json={'receipt_id':'vr_1'})
        return httpx.Response(200,json={})
    with DoneProofClient('https://dp.test',api_key='key-a',transport=httpx.MockTransport(handler)) as c:
        assert c.verify_run('cc_1',idempotency_key='task-1')['receipt_id']=='vr_1'
    req=seen[0]
    assert req.headers['X-DoneProof-Key']=='key-a'
    assert req.headers['Idempotency-Key']=='task-1'
