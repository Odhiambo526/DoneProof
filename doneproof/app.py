from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from .adapters.github import GitHubAdapter
from .adapters.mock import MockAdapter
from .compiler import AstraCompiler
from .domain import CompileRequest, CompletionContract, VerificationReceipt, VerifyRequest
from .engine import VerificationEngine
from .store import Store

app = FastAPI(
    title="DoneProof API",
    version="0.2.0",
    description="Independent outcome verification for AI agents.",
)
store = Store()
engine = VerificationEngine({"github": GitHubAdapter(), "mock": MockAdapter()})
compiler = AstraCompiler()


@app.get("/", response_class=HTMLResponse)
def landing():
    return """
<!doctype html><html><head><meta charset='utf-8'><title>DoneProof</title>
<style>body{font-family:system-ui;max-width:900px;margin:72px auto;padding:0 24px;background:#0b0d10;color:#eef2f6}code,pre{background:#161a20;padding:3px 7px;border-radius:6px}.hero{font-size:52px;line-height:1.05}.muted{color:#a9b4c0}.card{border:1px solid #29313a;border-radius:16px;padding:24px;margin-top:28px}</style></head>
<body><div class='hero'>Your agent says “done.”<br><b>DoneProof proves it.</b></div>
<p class='muted'>v0.2 — intent → completion contract → independent discovery/observation → signed verification receipt.</p>
<div class='card'><h2>Try the API</h2><p>Open <a href='/docs' style='color:#8ec5ff'>/docs</a>, run <code>POST /v1/verify/demo</code>, then inspect the signed receipt.</p></div>
</body></html>"""


@app.get("/health")
def health():
    return {"ok": True, "service": "doneproof", "version": "0.2.0"}


@app.post("/v1/contracts/compile", response_model=CompletionContract)
async def compile_contract(req: CompileRequest):
    try:
        contract = await compiler.compile(req.task, req.context, req.task_started_at)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.save_contract(contract)
    return contract


@app.post("/v1/verify", response_model=VerificationReceipt)
async def verify(req: VerifyRequest):
    store.save_contract(req.contract)
    receipt = await engine.verify(req.contract)
    store.save_receipt(receipt)
    return receipt


@app.post("/v1/verify/demo", response_model=VerificationReceipt)
async def verify_demo():
    contract = CompletionContract.model_validate(
        {
            "task": "Create GitHub issue 'Auth bypass' and assign alice",
            "postconditions": [
                {
                    "id": "p1",
                    "description": "Issue exists with requested title",
                    "provider": "mock",
                    "selector": {"state": {"title": "Auth bypass", "assignees": []}},
                    "predicate": {"op": "eq", "path": "title", "expected": "Auth bypass"},
                    "required": True,
                },
                {
                    "id": "p2",
                    "description": "Alice is assigned",
                    "provider": "mock",
                    "selector": {"state": {"title": "Auth bypass", "assignees": []}},
                    "predicate": {"op": "contains", "path": "assignees", "expected": "alice"},
                    "required": True,
                },
            ],
        }
    )
    store.save_contract(contract)
    receipt = await engine.verify(contract)
    store.save_receipt(receipt)
    return receipt


@app.get("/v1/receipts", response_model=list[VerificationReceipt])
def receipts(limit: int = Query(default=50, ge=1, le=200)):
    return store.list_receipts(limit)
