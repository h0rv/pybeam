# Agent framework demo: what PyBEAM adds

**Recommendation, 2026-11-19:** Use LangGraph for the broadest Python audience. Show a fleet of long-lived agent sessions, a specialist handoff, and a replacement process that resumes from an acknowledged checkpoint. Handoffs alone aren't a reason to adopt PyBEAM. Neither is a slow, cancellable tool: plain asyncio handled that scenario easily, and the PyBEAM diagnostic restart actually delayed new calls to healthy actors in the same group. The stronger next demo is live sessions that survive the loss of an entire worker, with a real process-isolated tool and a visible recovery boundary.

## Which frameworks ship

Ranked by **30-day PyPI downloads**, not production market share. PyPI totals and GitHub stars are snapshots retrieved 2026-11-19. Release dates are the first upload of the latest version returned by PyPI that day. Downloads include CI, transitive dependencies and repeat downloads; GitHub stars represent interest. The LangChain and LangGraph rows overlap and must not be added. Smaller core/slim packages and other language SDKs are not counted. Case studies are vendor-published, not independent audits.

| Package (PyPI stats) | 30-day downloads | Repository stars | Latest release (UTC) | Deployment evidence |
| --- | ---: | ---: | --- | --- |
| [LangChain](https://pypistats.org/packages/langchain) | 156,884,331 | [149,849](https://github.com/langchain-ai/langchain) | [1.5.2, Nov 18](https://pypi.org/project/langchain/1.5.2/) | A distinct cluster in the [LangChain survey](https://www.langchain.com/state-of-agent-engineering) reported building on LangChain/LangGraph. That doesn't identify deployment counts for either package. |
| [LangGraph](https://pypistats.org/packages/langgraph) | 60,374,924 | [45,130](https://github.com/langchain-ai/langgraph) | [1.2.22, Nov 14](https://pypi.org/project/langgraph/1.2.22/) | [Klarna customer service (Feb 2025)](https://www.langchain.com/blog/customers-klarna) and [AppFolio Realm-X (Dec 2024)](https://www.langchain.com/blog/customers-appfolio) explicitly name LangGraph. |
| [OpenAI Agents SDK](https://pypistats.org/packages/openai-agents) | 19,819,745 | [30,755](https://github.com/openai/openai-agents-python) | [0.25.0, Nov 18](https://pypi.org/project/openai-agents/0.25.0/) | [OpenAI's successor to Swarm](https://openai.github.io/openai-agents-python/) is the closest Python analogue for handoffs. No named production case verified here; using OpenAI models does not establish use of this SDK. |
| [Google ADK](https://pypistats.org/packages/google-adk) | 10,918,898 | [22,265](https://github.com/google/adk-python) | [2.14.1, Nov 12](https://pypi.org/project/google-adk/2.14.1/) | [Google's docs](https://google.github.io/adk-docs/) establish deployment support, not named use of the Python SDK. |
| [Pydantic AI](https://pypistats.org/packages/pydantic-ai) | 6,925,252 | [21,279](https://github.com/pydantic/pydantic-ai) | [2.70.0, Nov 17](https://pypi.org/project/pydantic-ai/2.70.0/) | [SentinelOne Purple AI (Nov 2026)](https://pydantic.dev/case-studies/sentinelone), [Dosu's coding agents (Aug 2026)](https://pydantic.dev/case-studies/dosu) and [General Intelligence Company (May 2026)](https://pydantic.dev/case-studies/gic) explicitly name the framework, not just Logfire. |
| [CrewAI](https://pypistats.org/packages/crewai) | 2,587,344 | [61,086](https://github.com/crewAIInc/crewAI) | [1.15.42, Nov 18](https://pypi.org/project/crewai/1.15.42/) | [CrewAI customer cases](https://www.crewai.com/case-studies) name PwC, IBM and AWS. The AWS writeup describes Bedrock reference architectures and a pipeline of rollouts, not proof that Bedrock itself runs on CrewAI. |
| [LlamaIndex](https://pypistats.org/packages/llama-index) | 1,803,535 | [52,946](https://github.com/run-llama/llama_index) | [0.14.26, Nov 13](https://pypi.org/project/llama-index/0.14.26/) | [Netchex AskHR + Netchex AI](https://www.llamaindex.ai/blog/case-study-netchex-more-efficient-hr-operations-with-llamaindex-powered-askhr-netchex-ai) names LlamaIndex for a customer-facing HR assistant. The use case doesn't establish a general orchestration market share. |
| [smolagents](https://pypistats.org/packages/smolagents) | 739,117 | [30,434](https://github.com/huggingface/smolagents) | [1.26.0, May 29](https://pypi.org/project/smolagents/1.26.0/) | [Hugging Face](https://huggingface.co/docs/smolagents/index) demonstrates code/tool agents; no named production deployment verified here. |

[State of Agent Engineering](https://www.langchain.com/state-of-agent-engineering), published June 12, 2026, surveyed 1,340 respondents in late 2025: 57.3% reported agents in production. This vendor-channel survey covers agents generally, not package-level production share. LangGraph wins for this demo by both reach and relevant named deployments. Pydantic AI is a credible second port, particularly for teams that want typed Python tools. Its [Azure Durable Task integration](https://pydantic.dev/articles/azure-durable-task-pydantic-ai) already addresses crash recovery for workflows. Similarly, [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) already provides checkpointed workflow resumption: a saved transcript alone is not a PyBEAM differentiator.

## The Elixir Swarm repo

[SwarmEx](https://github.com/nrrso/swarm_ex) is the literal match: its GitHub description explicitly says it is inspired by OpenAI Swarm. It had 90 stars, 20 commits and a [latest main commit dated Nov 2, 2024](https://github.com/nrrso/swarm_ex/commits/main/). [Jido](https://github.com/agentjido/jido) (1,919 stars) and [Jido.AI](https://github.com/agentjido/jido_ai) are better design references for the supervision idea, but they're not the literal Swarm port.

| Demo or capability | What the Elixir code uses | Plain asyncio difficulty |
| --- | --- | --- |
| SwarmEx [Star Wars Livebook](https://github.com/nrrso/swarm_ex/blob/main/livebooks/swarm_ex.livemd): a tool classifies messages, triage answers ordinary questions and sends Star Wars questions to a specialist | Elixir agent API and message routing; [agents](https://github.com/nrrso/swarm_ex/blob/main/lib/swarm_ex/agent.ex) are GenServers with a Registry address. The branch decision doesn't need any BEAM primitive. | **Easy.** A graph branch or a handoff callback reproduces the user-visible behavior. |
| SwarmEx runtime: named agents, serialized state, local restart and telemetry | [Application](https://github.com/nrrso/swarm_ex/blob/main/lib/swarm_ex/application.ex) has a Registry and a `one_for_one` DynamicSupervisor with a restart budget. Agent hooks include health and recovery; that isn't proof of persistent state on node loss. | **Easy** for data ownership with one queue consumer per agent. **Harder** once adding restart budgets, generation checks, bounded admission and no replay of uncertain calls. |
| Jido.AI [weather context, tool calls and reasoning examples](https://github.com/agentjido/jido_ai#runnable-examples) | Model and tool orchestration; Jido's [AgentServer](https://github.com/agentjido/jido/blob/main/guides/runtime.md) adds GenServer identity, routing and ownership. | **Easy** for the notebook behavior. Running thousands of addressed, independently supervised sessions is a separate problem. |
| Jido [parent/child agents and durable pods](https://github.com/agentjido/jido/blob/main/guides/pods.md): start and replace children; save topology and wake agents on demand | AgentServer, OTP lifecycle supervision and explicit storage/lazy activation. The state-machine portion is separable from the OTP runtime. | **Hard** to replicate the integrated lifecycle, persistence and cleanup in just asyncio; none is impossible if you add storage, supervisors and coordination. |

I found no SwarmEx demo proving cross-node takeover, hot code upgrades, or millions of agents. These shouldn't be inferred from a GenServer or from generic BEAM capability claims. I read the Elixir source and examples; I did not run either Elixir project.

## What the probe actually showed

[`probe.py`](probe.py) uses LangGraph 1.2.22 and LangChain Core's `FakeListChatModel` in both lanes. The model is scripted to emit the billing route; tracing is disabled. The stalled tool is a simulated async tool body (not an LLM-selected tool call). Both lanes begin it, complete the healthy sessions' turns, and dispatch a billing handoff while it is outstanding. In PyBEAM the handoff is a bounded Hornbeam pub/sub notification; the controller routes it to the specialist, whose identity and transcript are checked. In asyncio the handoff uses a bounded in-process queue.

The PyBEAM target uses `ActorGroup.restore` and `write_state` to commit a transcript, then adds an uncommitted draft. Cancelling the waiting caller leaves one native request admitted. Diagnostic `ActorGroup.crash` kills the BEAM child; OTP replaces it after the outstanding tool body drains. The controller uses `rejoin` and checks the generation changed, only the committed transcript returned, and the healthy specialist retained its process identity and state. The asyncio lane cancels the actual cooperative tool coroutine, creates a blank session, and shows that a caller-owned in-memory checkpoint is enough for this same-process scenario. That checkpoint is deliberately **not** equivalent to PyBEAM's on-disk snapshot across VM loss. There is no failing Python process in this experiment.

Linux x86-64 (8 vCPU), Python 3.14.10, OTP 29.1.2, PyBEAM base `5dcfb9c`. One run per lane and size, milliseconds; these are scenario timings, not throughput benchmarks. PyBEAM has one extra actor in a separate group as a control. The peer batch timing excludes the handoff and recovery. Crash recovery includes waiting for the remainder of the three-second tool, unlike asyncio cancellation.

| Sessions | Healthy turns | asyncio batch | PyBEAM spawn | PyBEAM batch | PyBEAM recovery | Healthy read during crash, same / other group |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 128 | 127 | 289.9 | 314.7 | 536.0 | 2,475.1 | 2,423.6 / 102.2 |
| 512 | 511 | 1,141.2 | 461.1 | 1,327.2 | 1,698.3 | 1,647.0 / 101.3 |

Both asyncio runs recovered the in-memory checkpoint after cancellation in under 0.05 ms (not a VM recovery). The same-group control read took almost the entire remaining drain time, while the other group replied before recovery. [The pinned native code](https://github.com/h0rv/erlang-python/blob/5b591e9b04c9cce8fa6139ca1486dfa203095b26/src/py_actor_group.erl#L106-L119) handles crash synchronously and waits for drain; [reference calls](https://github.com/h0rv/erlang-python/blob/5b591e9b04c9cce8fa6139ca1486dfa203095b26/priv/_erlang_impl/_actor_data.py#L389-L394) [resolve](https://github.com/h0rv/erlang-python/blob/5b591e9b04c9cce8fa6139ca1486dfa203095b26/priv/_erlang_impl/_actor_data.py#L349-L361) through [that same group server](https://github.com/h0rv/erlang-python/blob/5b591e9b04c9cce8fa6139ca1486dfa203095b26/src/py_actor_group.erl#L24-L26) before admission. In a separate exploratory run with an unbounded `Event.wait()`, OTP logged the kill but recovery and shutdown did not finish for at least 73 seconds; the run was stopped. Changing the same tool to a finite wait allowed recovery. The diagnostic is not a way to preempt an arbitrary Python tool or provide native crash isolation.

**Gaps that matter to the pitch:** Rejoin is explicit; in-flight work is not replayed; snapshot and pub/sub notification are not atomic; pub/sub isn't a durable handoff. Peer batches progressed before the crash, not independently of a shared group's drain. No ordinary tool exception, blocking sync/native tool, dedicated process, host loss, partition, presence, or end-to-end durable job was measured. Keep tools in a killable process when strict termination is needed. If a second demo can't show healthy agents continuing through a real worker loss and one agent resuming without repeating an external effect, pitch the primitives as useful building blocks, not an obvious framework upgrade.

## Reproduce from a developer checkout

Run the following from the PyBEAM checkout root. The development build uses the pinned native dependencies. The isolated scratch directory has local actor state; use a new one for a fresh comparison.

```sh
mise trust
mise install
mise exec -- uv run --locked python tools/build_native.py
mise exec -- uv pip install --python .venv/bin/python 'langgraph==1.2.22' 'langchain-core==1.8.3'

root="$PWD"
scratch="$(mktemp -d)"
printf '[tool.pybeam]\ndevelopment_runtime = "%s/_build/hornbeam"\ndata_dir = "state"\n' \
  "$root" > "$scratch/pyproject.toml"
cd "$scratch"
for sessions in 128 512; do
  "$root/.venv/bin/python" "$root/docs/research/probes/agents/probe.py" asyncio --sessions "$sessions"
  PYTHONPATH="$root/_build/hornbeam/lib/hornbeam/priv" \
    mise exec erlang@29.1.2 -- "$root/.venv/bin/python" \
    "$root/docs/research/probes/agents/probe.py" pybeam --sessions "$sessions"
done
```

The supervisor's `reason: killed` report is expected. The summary must include `handoff=billing` in both lanes, and the PyBEAM lane must show a changed generation and `transcript=['customer=42']`.
