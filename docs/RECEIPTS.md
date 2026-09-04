# Verification receipts

A DoneProof receipt is portable evidence of what was checked and what was observed.

## Assurance levels

- `registered` — DoneProof registered the run and timestamp before execution. Recommended.
- `submitted` — a complete contract was supplied at verification time. Useful for imports and retrospective checks but has a weaker timing trust boundary.
- `synthetic` — demo/test evidence only.

## Cryptographic fields

- `receipt_hash` — SHA-256 of the canonical receipt payload excluding the hash and signature fields.
- `signature_alg` — `Ed25519`.
- `key_id` — short fingerprint of the public key.
- `public_key` — Base64 raw Ed25519 public key.
- `signature` — Base64 Ed25519 signature.

The service's current signing key can be fetched from:

```text
GET /v1/signing-key
```

A receipt can self-check cryptographic integrity, but trust in the signer depends on obtaining the expected public key through a trusted deployment channel.

## Data minimization

Condition evidence records the value addressed by the predicate, not the entire provider object. This reduces unnecessary exposure of mailbox, repository or business-system state.
