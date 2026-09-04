import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import httpx

from doneproof.adapters.gmail import GmailAdapter
from doneproof.domain import CompletionContract, Verdict
from doneproof.engine import VerificationEngine
from doneproof.signing import ReceiptSigner

START = datetime(2026,9,4,3,0,0,tzinfo=timezone.utc)


def gmail_message(mid="m1", labels=None, subject="Invoice", to="alice@example.com", attachments=None):
    parts=[{"filename":name,"body":{"attachmentId":"a"}} for name in (attachments or [])]
    return {"id":mid,"threadId":"t1","labelIds":labels or ["SENT"],"internalDate":str(int(datetime(2026,9,4,3,1,tzinfo=timezone.utc).timestamp()*1000)),"payload":{"headers":[{"name":"Subject","value":subject},{"name":"To","value":to},{"name":"From","value":"agent@example.com"}],"parts":parts}}


def contract(location="sent"):
    return CompletionContract.model_validate({"task":"Send invoice to Alice","task_started_at":START.isoformat(),"postconditions":[
        {"id":"p1","description":"message is sent","provider":"gmail","selector":{"message_id":None,"subject":"Invoice","to":"alice@example.com"},"predicate":{"op":"eq","path":"location","expected":location},"required":True},
        {"id":"p2","description":"invoice attached","provider":"gmail","selector":{"message_id":None,"subject":"Invoice","to":"alice@example.com"},"predicate":{"op":"contains","path":"attachment_names","expected":"invoice.pdf"},"required":True},
    ]})


def run(handler, settings, c=None):
    s=replace(settings,gmail_access_token="token")
    adapter=GmailAdapter(s,transport=httpx.MockTransport(handler))
    return asyncio.run(VerificationEngine({"gmail":adapter},ReceiptSigner(s),timeout_seconds=2).verify(c or contract()))


def test_gmail_sent_message_is_verified(settings):
    msg=gmail_message(attachments=["invoice.pdf"])
    def handler(request):
        if request.url.path.endswith('/messages'): return httpx.Response(200,json={"messages":[{"id":"m1"}]})
        return httpx.Response(200,json=msg)
    assert run(handler,settings).verdict == Verdict.VERIFIED


def test_gmail_draft_is_not_mistaken_for_sent(settings):
    msg=gmail_message(labels=["DRAFT"],attachments=["invoice.pdf"])
    def handler(request):
        if request.url.path.endswith('/messages'): return httpx.Response(200,json={"messages":[{"id":"m1"}]})
        return httpx.Response(200,json=msg)
    assert run(handler,settings).verdict == Verdict.PARTIAL


def test_gmail_duplicate_candidates_are_unknown(settings):
    one=gmail_message(mid="m1",attachments=["invoice.pdf"]); two=gmail_message(mid="m2",attachments=["invoice.pdf"])
    def handler(request):
        if request.url.path.endswith('/messages'): return httpx.Response(200,json={"messages":[{"id":"m1"},{"id":"m2"}]})
        return httpx.Response(200,json=one if request.url.path.endswith('/m1') else two)
    assert run(handler,settings).verdict == Verdict.UNKNOWN


def test_unconnected_gmail_is_unknown(settings):
    adapter=GmailAdapter(settings)
    r=asyncio.run(VerificationEngine({"gmail":adapter},ReceiptSigner(settings),timeout_seconds=1).verify(contract()))
    assert r.verdict == Verdict.UNKNOWN
