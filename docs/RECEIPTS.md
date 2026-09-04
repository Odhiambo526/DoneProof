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

A receipt is self-verifying. Historical receipts remain cryptographically verifiable after DoneProof rotates to a new signing key.

The deployment's **current** public signing key is available at:

```text
GET /v1/signing-key
```

For a specific receipt, use the key embedded in that receipt or its evidence bundle.

## Evidence bundle

```text
GET /v1/receipts/{receipt_id}/bundle
```

returns:

- the signed receipt
- an integrity result
- the public key that signed that receipt

This is the preferred portable export for pilot archives and audit review.

## Data minimization

DoneProof stores the value addressed by a predicate rather than the entire provider object whenever possible. Gmail message bodies, provider access tokens and execution-agent secrets are not written into verification receipts.

## Trust in the signer

Cryptographic integrity proves that a receipt has not changed since DoneProof signed it. An external auditor must still establish that the expected DoneProof deployment/public key is trusted through an independent channel.
