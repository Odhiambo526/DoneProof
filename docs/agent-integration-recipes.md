# Thin agent integration recipes

These recipes wrap customer-owned runtimes. DoneProof neither executes the agent
nor consumes its traces, reasoning, screenshots or tool results. Prepare once,
persist the session ID, execute externally, then independently verify. On an
agent exception, decide whether to verify partial external effects; never turn
the exception or success message into evidence.

With an existing `AsyncDoneProof` client `dp`, a customer task and a stable
business operation ID, the generic boundary is:

```python
async def assure(dp, task, operation_id, execute):
    session = await dp.assurance.prepare(task=task, idempotency_key=operation_id)
    if session.state != 'READY_FOR_EXECUTION':
        return session
    await execute()
    return await dp.assurance.verify(session.id, wait=True)
```

The caller owns `execute`; no output is forwarded to DoneProof. Use your durable
workflow store to resume verification from the saved session ID after a crash.
Do not re-execute the business action just because polling timed out.

## OpenAI Agents SDK

For an existing Python agent, supply `lambda: Runner.run(agent, task)` to the
generic wrapper. For TypeScript, prepare using DoneProof, call
`await run(agent, task)` from `@openai/agents`, then call DoneProof verify. Agent
results and tracing remain in your existing runtime. These execution entry
points follow the [official running-agents guide](https://developers.openai.com/api/docs/guides/agents/running-agents).

## LangGraph

For an already compiled graph, pass `lambda: graph.ainvoke(inputs)` to the generic
wrapper. If the graph pauses for an interrupt, resume it in your own runtime
before requesting final verification; an interrupt is not task completion.
See the [LangGraph graph API guide](https://docs.langchain.com/oss/python/langgraph/use-graph-api).

## CrewAI

For an existing crew, pass `lambda: crew.akickoff(inputs=inputs)` when using
native async CrewAI. Existing applications using `kickoff_async` can keep their
thread-based execution method. DoneProof does not inspect crew metrics or memory.
See [CrewAI execution methods](https://docs.crewai.com/en/concepts/crews).

## Generic Node agent

```ts
async function assure(dp, task, operationId, execute) {
  const session = await dp.assurance.prepare({ task, idempotencyKey: operationId });
  if (session.state !== 'READY_FOR_EXECUTION') return session;
  await execute();
  return dp.assurance.verify(session.id, { wait: true });
}
```

Framework APIs are intentionally documented recipes, not pinned runtime
dependencies of DoneProof. The maintained executable tests exercise the
framework-neutral HTTP/SDK boundary and explicit fixture agents.
