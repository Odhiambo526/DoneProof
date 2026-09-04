# Verification receipts

A DoneProof receipt is a portable, signed record of what was verified, against which contract, and what evidence supported the verdict.

## Assurance levels

- `registered` — DoneProof registered the run before execution. Recommended for live workflows.
- `submitted` — the contract was supplied at verification time. Useful for imports and retrospective checks with a weaker timing boundary.

Internal test/demo assurance modes are not part of the customer API contract.

## Integrity fields

- `contract_hash` — SHA-256 of the exact canonical completion contract.
- `receipt_hash` — SHA-256 of the canonical signed receipt payload, excluding the receipt hash and signature fields.
- `signature_alg` — `Ed25519`.
- `key_id` — fingerprint of the receipt's public key.
- `public_key` — Base64 raw Ed25519 public key used for this receipt.
- `signature` — Base64 Ed25519 signature.

A receipt is self-contained for **integrity checking**: its embedded key can prove that the receipt has not changed since it was signed. The embedded key alone does **not** authenticate the issuer, because any party can generate its own Ed25519 key pair. For issuer authenticity, verify the receipt against a DoneProof public key that your organization pinned through an independent channel.

The deployment's **current** public signing key is available at:

```text
GET /v1/signing-key
```

For a specific receipt, the embedded key or evidence bundle is sufficient for integrity checks. For audit, authorization or compliance decisions, compare that key with a separately pinned trusted DoneProof key.


## Trusted offline verification

Pin the deployment key once through a trusted onboarding channel, then verify receipts against that exact key:

```python
from doneproof.client import DoneProofClient

trusted_key = "<base64 Ed25519 public key pinned during onboarding>"
valid = DoneProofClient.verify_receipt(receipt, trusted_key)
```

`ReceiptSigner.verify(receipt)` checks integrity against the key carried by the receipt. `ReceiptSigner.verify_trusted(receipt, trusted_key)` additionally requires that signer to match the independently trusted key.

## Evidence bundle

```text
GET /v1/receipts/{receipt_id}/bundle
```

returns:

- the signed receipt
- an integrity result
- the public key that signed that receipt

This is the preferred portable export for pilot archives and audit review. Treat the bundle as evidence plus signature material, not as its own trust anchor.

## Data minimization

DoneProof stores the value addressed by a predicate rather than the entire provider object whenever possible. Gmail message bodies, provider access tokens and execution-agent secrets are not written into verification receipts.

## Trust in the signer

Cryptographic integrity proves that a receipt has not changed since DoneProof signed it. An external auditor must still establish that the expected DoneProof deployment/public key is trusted through an independent channel.
