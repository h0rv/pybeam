# Development

Install uv (or the tools pinned in `mise.toml`), then from a clean checkout:

```sh
uv sync --locked
uv run --locked --all-extras ty check --error-on-warning
```

The type check installs the optional framework dependencies too. No sibling
checkouts or native build are needed for these commands. Hornbeam is not on
PyPI; its optional extra uses a pinned commit from `h0rv/hornbeam`'s
`mvp/runtime-support` branch. Local stubs in `typings` cover the native
erlang-python interfaces needed for checking; they do not provide a runtime.
Keep them aligned with `h0rv/erlang-python`'s `mvp/native-channel-receive` branch.

For runtime development, clone those branches next to this checkout:

```sh
git clone --branch mvp/runtime-support https://github.com/h0rv/hornbeam.git ../hornbeam
git clone --branch mvp/native-channel-receive https://github.com/h0rv/erlang-python.git ../erlang-python
uv add --optional hornbeam --editable ../hornbeam
uv sync --locked --all-extras
uv run --locked --all-extras ty check --error-on-warning
```

`uv add` opts this checkout into editable Hornbeam by updating `pyproject.toml`
and `uv.lock`; keep those local overrides out of commits. Restore the committed
Hornbeam source and lock entries to return to the pinned default. `mise run setup`
also opts into editable Hornbeam before building the native runtime; that full
build additionally needs the runtime's other native dependencies and toolchain.
