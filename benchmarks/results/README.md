# Orchestration benchmark method

Run `python benchmarks/benchmark_orchestration.py --mode baseline|sync|durable --repeats 10 --output <file>`. Durable PostgreSQL runs also pass `--postgres` and an isolated test server's `TEST_DATABASE_URL`. The script creates and removes its own schema; it never reads customer data. SQLite runs use a temporary database.

Each 1, 10, 100 and 1,000-condition workload has one warmup plus ten measured runs. Two synthetic observation delays are tested: zero and 5ms. Results include p50, p95, mean, peak provider concurrency, Python/platform and capture time. Windows timer granularity can turn a requested 5ms sleep into roughly 15ms; compare before/after on the same platform.

The preserved `phase1_engine.py` is the engine from Phase 1 commit `c33ad5f5918d36300d3581e700dffcb39c940ba9`. Baseline and new synchronous measurements cover observation, evaluation and signing, with no database or real network. The 100/1,000-condition engine benchmarks deliberately bypass the legacy API's 50-condition validator; they do not imply those calls were supported by that API.

Durable measurements include job creation, provider/job leases, attempt and observation checkpoints, evaluation, signing, receipt persistence and the final receipt read. This is additional work, so durable timings must not be described as a pure engine speed comparison. All observations come from an in-process authoritative stub; these are reproducible orchestration costs, not a claim about live Gmail/GitHub latency or throughput.

`phase2-before-windows.json` was captured before the refactor. The `phase2-after-*-windows.json` files use the same local environment. CI runs all three modes sequentially on a PostgreSQL 16 runner and uploads the JSON summaries as the `orchestration-benchmarks` artifact. The new limit intentionally trades single-job latency for bounded provider load; peak concurrency is part of the result, not an omitted side effect.
