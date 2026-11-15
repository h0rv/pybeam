# Isolated static API probes

These are proposals, not implemented actors. `candidates.py` contains abstract class declarations and reference protocols. Nothing constructs an actor, sends a message, or dispatches application operations by string. No proxy is asserted to have an actor's type.

## Reproduce

Run from the repository root. The commands use the existing environment without syncing or changing dependencies. Pyright is an isolated research tool, not a project dependency. Production checking remains ty with all configured rules treated as errors.

Observed versions were ty `0.0.104` (`b4bb5b2c8`, 2026-11-13), Ruff `0.17.5`, and Pyright `1.1.414`. Both checkers target Python 3.14. Version checks actually run were:

```sh
uv run --no-sync ty --version
uv run --no-sync ruff --version
uvx --from pyright pyright --version
```

Final lint, formatting, and positive commands actually run were:

```sh
uv run --no-sync ruff format docs/research/probes
uv run --no-sync ruff check docs/research/probes
uv run --no-sync ty check --error-on-warning docs/research/probes/candidates.py docs/research/probes/positive.py
uvx --from pyright==1.1.414 pyright --warnings --pythonversion 3.14 docs/research/probes/candidates.py docs/research/probes/positive.py
```

Lint and positive checks passed. Pyright reported zero errors and zero warnings. `assert_type` checks establish an awaited `int` result for each call shape. Additional positives show that an actor class can structurally satisfy `CounterRef` and that the generic callable contract accepts a free function with a compatible receiver. Neither observation establishes a remote reference or an allowed actor operation.

Negative commands actually run were:

```sh
uv run --no-sync ty check --output-format concise --error-on-warning docs/research/probes/negative_generic.py docs/research/probes/negative_direct.py docs/research/probes/negative_explicit.py
uvx --from pyright==1.1.414 pyright --warnings --pythonversion 3.14 docs/research/probes/negative_generic.py docs/research/probes/negative_direct.py docs/research/probes/negative_explicit.py
```

Both commands intentionally fail with verified exit status 1. Do not add these files to a passing production type gate. All negative functions must be rejected, not merely enough functions to produce a nonzero exit. ty reported 27 error diagnostics. Pyright reported 27 errors and zero warnings.

## Expected negative diagnostics

Each negative module tests the following functions. Line numbers are shared across modules except the generic bound method case.

| Function | Line | ty | Pyright |
| --- | --- | --- | --- |
| `wrong_receiver` | 5 | `invalid-argument-type` for generic/direct; `invalid-explicit-return` for explicit | `reportArgumentType` for generic/direct; `reportReturnType` for explicit |
| `wrong_positional` | 9 | `invalid-argument-type` | `reportArgumentType` |
| `wrong_keyword_value` | 13 | `invalid-argument-type` | `reportArgumentType` |
| `wrong_keyword_name` | 17 | `missing-argument` and `unknown-argument` | Two `reportCallIssue` errors |
| `missing_keyword` | 21 | `missing-argument` | `reportCallIssue` |
| `positional_only` | 25 | `positional-only-parameter-as-kwarg` | `reportCallIssue` |
| `wrong_return` | 29 | `invalid-explicit-return` | `reportReturnType` |
| `bound_method`, generic only | 33 | `invalid-argument-type` | `reportArgumentType` and `reportCallIssue` |

For the generic wrong receiver and bound method, ty also reports `unsound-return` because failed inference leaves an unknown result. Pyright additionally reports `reportAbstractUsage` for the direct unbound invocation of the abstract method. These extra errors do not replace the receiver errors. Counts by file are ty 11 generic, 8 direct, 8 explicit; Pyright 10 generic, 9 direct, 8 explicit.

## Limits

The probes establish the listed static facts for these checker versions and one method signature. They do not establish a transport implementation or prove actor membership. `CounterRef` is structural, not a remote identity marker. `Counter` is an abstract declaration, not a proxy returned from spawning. Abstract methods raise `NotImplementedError`; they are not local actor implementations.

No actor runtime, runtime test, framework integration test, dependency isolation test, or Python probe execution was run. Overloads, descriptors, inherited generic methods, and more complex ParamSpec inference were not tested. Pyright used its normal CLI checking mode with warnings enforced, not an added strict project configuration. No serialization, cancellation, mailbox scheduling, restart, cross-node compatibility, or isolation claim follows from passing these checks. In particular, `int` typing does not establish transport range limits or distinguish booleans from integers.
