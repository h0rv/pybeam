# Development

Install uv (or the tools pinned in `mise.toml`), then from a clean checkout:

```sh
uv sync --locked
uv run --locked --all-extras ty check --error-on-warning
```

The type check installs the optional framework dependencies too. No sibling
checkouts or native build are needed for these commands. Hornbeam is not on
PyPI; its optional extra locks a commit from `h0rv/hornbeam`'s
`main` branch. Local stubs in `typings` cover the native
erlang-python interfaces needed for checking; they do not provide a runtime.
Keep them aligned with the erlang-python commit pinned in `rebar.lock`.

For runtime development, clone those branches next to this checkout:

```sh
git clone --branch main https://github.com/h0rv/hornbeam.git ../hornbeam
git clone --branch main https://github.com/h0rv/erlang-python.git ../erlang-python
runtime_ref=$(sed -n '/<<"erlang_python">>/,/{ref/s/.*{ref,"\([^"]*\)"}.*/\1/p' rebar.lock)
git -C ../erlang-python checkout "$runtime_ref"
uv add --optional hornbeam --editable ../hornbeam
uv sync --locked --all-extras
uv run --locked --all-extras ty check --error-on-warning
```

`uv add` opts this checkout into editable Hornbeam by updating `pyproject.toml`
and `uv.lock`; keep those local overrides out of commits. Restore the committed
Hornbeam source and lock entries to return to the pinned default. `mise run setup`
also opts into editable Hornbeam before building the native runtime; that full
build additionally needs the runtime's other native dependencies and toolchain.

## Runtime requirements

`pybeam.run` needs a managed runtime: either a `python-beam` wheel with bundled
runtime assets, or an already-built development runtime. A source install alone
does not include ERTS or the native extension. Packaged wheels currently support
macOS arm64 and Linux x86_64 on conventional CPython 3.14.

Native builds use CPython 3.14.10, Erlang 29.1.2, rebar3 3.28.0 and CMake 4.5.0
as pinned in `mise.toml`. With these tools and the sibling checkouts above
(plus `erlang-ws` and `erlang-h1`), build with `mise run setup`.

### Use a development runtime

Build the native runtime with `tools/build_native.py`, then add this to the
consumer project's `pyproject.toml` (absolute, or relative to that file):

```toml
[tool.pybeam]
development_runtime = "/path/to/pybeam/_build/hornbeam"
```

The directory is the build profile, not an executable or source checkout. It
must contain `lib/*/ebin` or `checkouts/*/ebin` directories with
`py_launch.beam` and `erlang_python.app`, and exactly one matching `py_nif.so`
or `py_nif.dylib` in that application's sibling `priv` directory. The native
library must match the host Python, and `erl` must be on `PATH`. Nothing is
built or downloaded implicitly.

### Build a Linux wheel

Linux wheels need conventional CPython 3.14 with a shared
`libpython3.14.so.1.0`. Build Erlang with static OpenSSL and without termcap so
the runtime needs only glibc, libm, libstdc++, libgcc_s and zlib; the wheel is
then tagged `manylinux_<glibc>_x86_64` for the newest glibc symbol it uses.
An Erlang that links libtinfo or libcrypto produces a `linux_x86_64` wheel,
which PyPI rejects. Releases build on Ubuntu 22.04.

```sh
KERL_CONFIGURE_OPTIONS="--disable-dynamic-ssl-lib --without-termcap --without-javac --without-wx --without-odbc" \
  MISE_ALL_COMPILE=true mise install
mise exec -- uv sync --locked --all-extras
mise exec -- uv run --locked python tools/build_native.py
mkdir -p build
curl -fL https://raw.githubusercontent.com/erlang/otp/OTP-29.1.2/LICENSE.txt -o build/OTP-LICENSE.txt
mise exec -- uv run --locked python tools/build_runtime.py \
  --otp-root "$(mise where erlang)" \
  --native-app _build/default/lib/erlang_python \
  --otp-license build/OTP-LICENSE.txt \
  --native-license _build/default/lib/erlang_python/LICENSE
mise exec -- uv build --wheel
```

Install the `dist/python_beam-*-manylinux_*_x86_64.whl` wheel into a Python
3.14 environment; `python examples/hello.py` then runs without `erl`, rebar3 or
mise on `PATH`. Linux wheels do not bundle the optional GenStage payload;
Darwin wheels do.

### Pooled actor execution

All `ActorGroup` instances use erlang-python's node-wide worker pool. Groups
bound actor count and per-actor admitted work; they do not own execution threads.
The native `worker_pool_max` application environment caps the shared pool, which
starts at the available logical CPU count and grows on demand. Calls queue FIFO
when every worker is busy at the cap. Closing a group releases its actors, not
the pool contexts.

## Releases

`.github/workflows/release.yml` builds the sdist and the Linux x86_64 and
macOS arm64 wheels, and verifies each clean wheel install. Publishing a GitHub
release whose tag is `v` plus the project version uploads them to PyPI through
trusted publishing from the `pypi` environment; a manual run uploads only when
`publish` is selected.

The macOS wheel bundles GenStage, compiled in CI by `tools.build_genstage` from
the Elixir and GenStage commits in `src/pybeam/runtime/optional_specs/genstage.json`.
Elixir keeps absolute source paths in quoted literals (`quote location: :keep`) and
`System` embeds its build date, so the build always runs in `/tmp/pybeam-genstage`
with `SOURCE_DATE_EPOCH` set to the Elixir commit time and deterministic compiler
options. The payload also carries the OTP compiler from the runner's mise-built
Erlang, whose bytes differ between macOS and Linux, so the pin is the manifest of
the macOS CI build and a local Linux build will not match it. The output must match
the pinned manifest byte for byte. After changing a source pin, copy the
`genstage-manifest` artifact from the failed macOS release job into `genstage.json`
and review the diff.
