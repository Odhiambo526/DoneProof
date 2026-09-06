# Orchestration benchmark method

## Measured comparison

PostgreSQL 16 / Python 3.12.14 / Linux, ten measured runs after warmup, 5ms synthetic observations. All three modes ran sequentially on the same CI runner at commit `6b9584a`; [source run](https://github.com/Odhiambo526/DoneProof/actions/runs/33953694605).

| Conditions | Before engine p50 (ms) | After bounded engine p50 (ms) | Durable PostgreSQL end-to-end p50 (ms) | Peak observations before → after |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 5.328 | 5.395 | 116.986 | 1 → 1 |
| 10 | 5.827 | 6.257 | 135.414 | 10 → 10 |
| 100 | 9.842 | 45.347 | 534.356 | 100 → 16 |
| 1,000 | 48.344 | 416.007 | 4,775.408 | 1,000 → 16 |

The durable column includes persistence and recovery checkpoints; the engine columns do not. The synthetic provider uses the 16-slot unresolved-provider allocation. Real GitHub and Gmail limits are 8 and 4. This is an intentional reliability/concurrency tradeoff, not a speedup: at 1,000 conditions the bounded engine takes about 8.6× the baseline wall time, and PostgreSQL orchestration adds further cost.

With zero observation delay, before/after engine p50 values are 0.189/0.263, 0.578/0.999, 4.323/8.237 and 44.396/83.247ms. Durable PostgreSQL p50 values are 116.738, 130.356, 485.242 and 4,577.398ms respectively. Full p95, mean, capture metadata and provenance are in [before](phase2-before-ci.json), [after engine](phase2-after-sync-ci.json) and [after PostgreSQL](phase2-after-postgres-ci.json).

## Reproduction

Run `python benchmarks/benchmark_orchestration.py --mode baseline|sync|durable --repeats 10 --output <file>`. Durable PostgreSQL runs also pass `--postgres` and an isolated test server's `TEST_DATABASE_URL`. The script creates and removes its own schema; it never reads customer data. SQLite runs use a temporary database.

Each 1, 10, 100 and 1,000-condition workload has one warmup plus ten measured runs. Two synthetic observation delays are tested: zero and 5ms. Results include p50, p95, mean, peak provider concurrency, Python/platform and capture time. Windows timer granularity can turn a requested 5ms sleep into roughly 15ms; compare before/after on the same platform.

The preserved `phase1_engine.py` is the engine from Phase 1 commit `c33ad5f5918d36300d3581e700dffcb39c940ba9`. Baseline and new synchronous measurements cover observation, evaluation and signing, with no database or real network. The 100/1,000-condition engine benchmarks deliberately bypass the legacy API's 50-condition validator; they do not imply those calls were supported by that API.

Durable measurements include job creation, provider/job leases, attempt and observation checkpoints, evaluation, signing, receipt persistence and the final receipt read. This is additional work, so durable timings must not be described as a pure engine speed comparison. All observations come from an in-process authoritative stub; these are reproducible orchestration costs, not a claim about live Gmail/GitHub latency or throughput.

`phase2-before-windows.json` was captured before the refactor. The `phase2-after-*-windows.json` files use the same local environment. CI runs all three modes sequentially on a PostgreSQL 16 runner and uploads the JSON summaries as the `orchestration-benchmarks` artifact. The new limit intentionally trades single-job latency for bounded provider load; peak concurrency is part of the result, not an omitted side effect.
