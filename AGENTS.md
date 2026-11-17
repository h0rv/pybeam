# Project boundaries

This is a personal MVP. Do not add company references, project licenses, copyright notices, or generated reports. Preserve vendored third-party notices. GitHub forks and committed branch pushes under the user's personal `h0rv` account are authorized. Do not force-push, push to upstream repositories, or publish to PyPI without separate permission. Keep README content focused on using the examples, not naming discussions, agent activity, audit history, test counts, or branch bookkeeping.

Bring BEAM capabilities to Python developers, including users of Django, FastAPI, Redis, Celery, and SQLAlchemy; do not require an Elixir or LiveView mental model. Map the useful semantics to concise, first-class Python and asyncio APIs and familiar framework integrations. Users should not need BEAM knowledge or infrastructure plumbing for ordinary use; expose advanced operations separately. Simplicity must not imply free durability or conceal deployment and failure boundaries.

Keep PyBEAM thin. Before implementing a capability or adding an abstraction, check OTP, erlang-python, Hornbeam, and established BEAM libraries for an existing implementation. Prefer binding and reusing their semantics over recreating them in Python. Hide operational plumbing, not ownership, overload, cancellation, durability, or restart guarantees.

Concentrate complexity in integration boundaries, not application code. Keep the existing ASGI frameworks. Do not introduce a web framework, browser protocol, ORM, task queue, or workflow engine. Storage and authorization policy belong to applications.

## Current MVP

Use free-threaded CPython 3.14t workers by default, and isolated OS processes for code that might crash the interpreter. CPython 3.14.10t development builds opt in with `PYBEAM_ALLOW_FREE_THREADED=1`; packaged wheels remain conventional. Consult the [measured execution modes](docs/research/execution-modes.md) before choosing a backend or claiming parallelism: dedicated-loop tasks parallelize on the tested 3.14t build, shared-loop tasks do not, and full integration acceptance remains incomplete. Own-GIL subinterpreters are unsupported because common native extensions do not load in them. Evaluate newer Python releases (including 3.15+) against actual runtime and dependency contracts; do not present alternatives as already working throughout PyBEAM.

Read the [execution model](docs/research/execution-model.md) before changing concurrency claims or execution APIs. Distinguish BEAM identity, execution context, native thread, interpreter, event loop, OS process, node, and persistent storage. Python version is not live GIL state; one free-threaded asyncio loop is not multicore or distributed; subinterpreters are not native-crash isolation. Supervision, mailboxes, shared memory, and pub/sub do not provide durable jobs or state. Keep acceptance, cancellation, retry, recovery, and uncertain outcomes explicit, and recheck source-linked constraints when dependencies change.

The verification gate is two independent real WebSocket clients sharing BEAM state, atomic concurrent increments, pub/sub invalidations followed by fresh reads, and dependable disconnect, cancellation, shutdown, and slow-consumer cleanup. Unit tests and adapter counts do not establish runtime compatibility. Surface runtime blockers rather than concealing them with workarounds.

Use mise for development tools and tasks, uv for Python dependencies, and rebar3 for Erlang dependencies. Fix runtime source directly in branches based on upstream main. Do not export or apply patches through this repository. Keep runtime changes in their forks.

## Ownership

- erlang-python owns execution, contexts, embedding, channels, message passing, and interpreter safety.
- Hornbeam owns web serving, ASGI behavior, and its existing BEAM service implementations.
- This library owns Python-facing ergonomics, typing, lifecycle integration, and optional adapters. It must not duplicate execution or supervision management.
- Generic capabilities must not require Hornbeam's ASGI server unless their actual implementation requires it. The current counter/cache/pubsub slice uses Hornbeam's existing services.
- Follow actual upstream runtime contracts. Investigate existing support before proposing additions.

Keep one Python distribution. Core primitives belong in domain packages such as `pybeam.actors`, `pybeam.mailbox`, `pybeam.services`, `pybeam.supervision`, and `pybeam.runtime`. Optional framework and ecosystem adapters belong under `pybeam.integrations.<name>`. Hornbeam-specific web-service contracts belong to its integration, not core services. Keep initializers minimal. Core modules must not import integrations or optional frameworks, and framework adapters must not import other framework adapters. Avoid duplicate transport abstractions and speculative plugin systems.

## Documentation

Before writing, editing, or reviewing README content, guides, API prose, docstrings, or example explanations, read and apply the [documentation-writing skill](.agents/skills/documentation-writing/SKILL.md). Include that path in delegated documentation briefs, including resumed tasks. Its Python-first audience rules, example checks, and editing checklist apply to small changes too. Keep this skill in the repository so future sessions use the same writing standard.

## Python and examples

Every delegated task, including a resumed task, must begin by reading the `modern-python` skill and its relevant reference files. Agent briefs must include the skill's file path explicitly. Apply its design principles, not just its lint rules. Verify Python changes with Ruff and strict ty.

Python developers are the primary audience. The default installation is `uv add python-beam` and the public import is `pybeam`. User-facing examples must be pure Python and must not require Erlang source, escripts, rebar files, a separately configured VM, or knowledge of native launch commands. PyBEAM must own runtime startup and shutdown by default. Optional `[tool.pybeam]` settings and explicit API options may expose external runtimes and polyglot integration. Do not call an internal native prototype a usable public Python feature. Bundle and verify runtime dependencies before claiming a clean installation works without developer tools.

Use precise types, named records, and Literal or enums for closed values. No Any, casts, broad ignores, or dynamic attribute probing to conceal missing types. Use structured concurrency and explicit resource ownership. Bound buffers and expose overflow. Do not block event loops or silently fall back to Python memory.

Use absolute imports such as `from package.module import Name`, with short local names. Do not repeat full module paths in annotations. Provide useful framework dependency aliases in the integration rather than repeating Annotated/Depends expressions in every route.

FastAPI examples use module-level apps and top-level decorated routes. Let FastAPI and Pydantic parse and validate requests. Do not write custom form parsers, wire decoders, or redundant validation wrappers in examples. Use framework dependency injection and overrides.

Use each framework's documented HTML templating: FastAPI Jinja2Templates, Litestar's supported template engine integration, and PyView's existing renderer. Do not add HTML builder dependencies or template processors. Use HTMX 4 with matching extensions. No f-strings, relative imports, module docstrings, or unnecessary comments. Public API docstrings should explain semantics that types cannot.

Run Ruff lint and format, ty with errors and warnings enforced, unit tests, optional-dependency isolation tests, and the real runtime tests. Never call skipped or unrun checks passes. Keep useful regressions in tests, concise status in README, and transient logs in pytest's temporary directories. Do not create evidence directories, work logs, or report-generating scripts.

## Future direction, not MVP work

PyBEAM is the chosen project name. The target repository is `pybeam`, the PyPI distribution is `python-beam`, and Python imports use `pybeam`. Coordinate the checkout and package rename across feature branches; individual agents must not rename shared paths while work is active. The separate Hornbeam runtime fork keeps its name. Do not expand the current MVP to pursue the following research.

Place new feature worktrees under `~/projects/worktrees/pybeam/`. Leave active worktrees in place until their work finishes. After verified merges, remove merged worktrees and branches only after checking for uncommitted or unmerged work. Defer a full documentation pass until the APIs stabilize; keep runnable examples and tests current.

The user has also authorized actor, standalone service, mailbox, and supervision implementation on separate feature branches and worktrees. Do not merge these into the MVP branch or duplicate OTP scheduling/supervision. Track native tests and unknown production limits before claiming readiness.

The user has authorized independent developer-experience research under `docs/research/`, including a gallery plan, typed API probes, an observability guide, documentation structure, and a runtime gap analysis. These requested documents are allowed; do not generate unrelated reports. Research agents must not edit production code, runtime forks, dependency configuration, or package names.

- Typed actors: compare ActorRef[T] with ParamSpec/Concatenate method references against Python actor classes with asynchronous typed methods and supervised spawning. Establish complete checking of arguments and return types without application-level string dispatch.
- Inspect OTP processes and erlang-python's process-bound environments first. Do not assume an interpreter per actor or create a Python actor scheduler or supervision framework.
- Document mailbox ownership and boundedness, cancellation, failures, restarts, state lifetime, and remote-call semantics before proposing an actor API.
- Standalone background services: evaluate existing worker loops, channels, task submission, and OTP supervisors without Hornbeam or ASGI. Distinguish long-running workers from stateful actors. Do not invent durable queues or workflow execution.
- Investigate a broader Python-first BEAM name and package structure only when it will not disrupt ongoing implementation.

Respect delegated file ownership. Verify actual changes and reproduce review findings before reporting work complete. Keep explanations plain and concise.
