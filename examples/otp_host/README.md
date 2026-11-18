# Python library calls from an OTP application

The OTP application owns the Python runtime. An importable Python function uses the same PyBEAM actor API as a Python application. No HTTP service or RPC client is involved.

`library.py` parses a bounded JSON batch, calls Python's `statistics` library inside a supervised actor, and returns JSON. The actor group is closed before the function returns.

## From Python

```sh
python -m examples.otp_host.library
```

The module starts through `pybeam.run(main)`. Application code uses the existing API:

```python
async with pybeam.ActorGroup(max_children=1, max_pending=4, max_bytes=65_536) as group:
    analyzer = await group.spawn(Analyzer)
    result = await analyzer.summarize(batch)
```

These limits bound actors, admitted requests and encoded request bytes. They do not bound arbitrary Python allocations.

## From an existing OTP application

Include `erlang_python` in the host application's dependencies. The host's Python environment must provide PyBEAM, its Python dependencies and the application's module.

`pybeam_otp_host.erl` uses the existing `py_context` API. Supply the application's trusted Python package directories, including its installed Python dependencies when those are not already on the interpreter's search path:

```erlang
PythonPaths = [<<"/srv/my_application">>,
               <<"/srv/python/lib/python3.14/site-packages">>],
Request = <<"{\"values\":[2.0,4.0,6.0,8.0]}">>,
{ok, Json} = pybeam_otp_host:analyze(PythonPaths, Request).
```

The helper creates a context linked to the caller, adds the trusted paths before starting its loop, and submits `examples.otp_host.library:analyze`. Its cleanup stops that loop and destroys only the context it created. It does not stop the host application or its runtime.

While a context's managed loop is running, use `py_context:submit_await`, not `py_context:call`. The host helper shows the complete call and cleanup sequence.

This example uses a supervised actor. It does not measure parallel Python execution. A timeout does not undo completed library work. Input is limited to 65,536 JSON bytes and 512 finite samples.
