ExUnit.start(autorun: false, exclude: [:benchmark, :artifact])

defmodule PyBEAM.GenStageProducer do
  use GenStage

  def init({owner, count, action}) do
    {:producer,
     %{
       owner: owner,
       count: count,
       action: action,
       emitted: 0,
       completed: 0,
       peak: 0,
       cancelled: false
     }, buffer_size: 1}
  end

  def handle_demand(1, %{emitted: n, count: count} = state) when n < count do
    # There is exactly one subscription and one total credit, including processing.
    0 = state.emitted - state.completed
    payload = :json.encode(%{id: n, action: state.action, text: "héllo"}) |> IO.iodata_to_binary()
    true = byte_size(payload) <= 1024
    next = %{state | emitted: n + 1, peak: 1}
    {:noreply, [payload], next}
  end

  def handle_demand(1, state) do
    send(state.owner, :source_done)
    {:noreply, [], state}
  end

  def handle_call(:completed, _from, state) do
    1 = state.emitted - state.completed
    {:reply, :ok, [], %{state | completed: state.completed + 1}}
  end

  def handle_call(:stats, _from, state), do: {:reply, state, [], state}

  def handle_cancel(_reason, _from, state) do
    send(state.owner, :subscription_cancelled)
    {:noreply, [], %{state | cancelled: true}}
  end
end

defmodule PyBEAM.GenStageConsumer do
  use GenStage

  def init({owner, limit}) do
    {:ok, context} = :py_context.new(%{mode: :worker, context_owner: self()})
    {:consumer, %{owner: owner, context: context, limit: limit, completed: 0}}
  end

  def handle_subscribe(:producer, _opts, from, state) do
    GenStage.ask(from, 1)
    {:manual, state}
  end

  def handle_events([payload], {producer, _ref} = from, state) do
    started = System.monotonic_time(:nanosecond)
    result = :py_context.call(state.context, :genstage_callbacks, :process, [payload], %{}, 2000)
    elapsed = System.monotonic_time(:nanosecond) - started
    # Unexpected native/Python errors fail closed: no replacement demand or retry.
    case result do
      {:ok, response} when is_binary(response) and byte_size(response) <= 1024 ->
        :ok = GenStage.call(producer, :completed)
        send(state.owner, {:outcome, :json.decode(response), elapsed})
        completed = state.completed + 1

        if completed == state.limit do
          GenStage.cancel(from, :normal)
        else
          GenStage.ask(from, 1)
        end

        {:noreply, [], %{state | completed: completed}}

      error ->
        send(state.owner, {:callback_error, error})
        {:stop, {:callback_error, error}, state}
    end
  end

  def handle_cancel(_reason, _from, state), do: {:noreply, [], state}

  def handle_call(:context, _from, state), do: {:reply, state.context, [], state}

  def terminate(_reason, state), do: :py_context.stop(state.context)
end

defmodule PyBEAM.GenStageTest do
  use ExUnit.Case, async: false

  setup_all do
    Application.put_env(:erlang_python, :num_contexts, 1)
    {:ok, _} = Application.ensure_all_started(:erlang_python)
    {:ok, _} = Application.ensure_all_started(:gen_stage)
    :ok
  end

  defp pipeline(count, limit, action) do
    {:ok, producer} = GenStage.start_link(PyBEAM.GenStageProducer, {self(), count, action})
    {:ok, consumer} = GenStage.start_link(PyBEAM.GenStageConsumer, {self(), limit})
    context = GenStage.call(consumer, :context)
    monitor = Process.monitor(context)

    try do
      {:ok, _} =
        GenStage.sync_subscribe(consumer,
          to: producer,
          max_demand: 1,
          min_demand: 0,
          cancel: :temporary
        )

      outcomes =
        Enum.map(1..limit, fn _ ->
          assert_receive {:outcome, result, elapsed}, 5000
          {result, elapsed}
        end)

      assert_receive :subscription_cancelled, 5000
      stats = GenStage.call(producer, :stats)
      assert stats.emitted == limit
      assert stats.completed == limit
      assert stats.peak == 1
      assert stats.cancelled
      refute_receive {:outcome, _, _}, 100
      {outcomes, stats}
    after
      GenStage.stop(consumer)
      GenStage.stop(producer)
      assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 5000
    end
  end

  test "actual loaded interpreter and application versions" do
    {:ok, context} = :py_context.new(%{mode: :worker})

    try do
      assert {:ok, [3, 14, 10]} =
               :py_context.eval(context, "list(__import__('sys').version_info[:3])")

      assert Application.spec(:gen_stage, :vsn) == ~c"1.3.2"
      assert System.version() == "1.20.0"
      assert :erlang.system_info(:otp_release) == ~c"29"
      otp_version = Path.join([:code.root_dir(), "releases", "29", "OTP_VERSION"])
      assert File.read!(otp_version) |> String.trim() == "29.1.2"
      IO.puts("COMPAT Python=3.14.10 OTP=29.1.2 Elixir=1.20.0 GenStage=1.3.2")
    after
      :py_context.stop(context)
    end
  end

  test "slow callback retains the only credit; subscription cancellation stops production" do
    {outcomes, _} = pipeline(16, 3, "slow")
    assert Enum.map(outcomes, fn {result, _} -> result["status"] end) == ["ok", "ok", "ok"]

    assert Enum.all?(outcomes, fn {result, elapsed} ->
             result["python_ns"] >= 50_000_000 and elapsed >= 50_000_000
           end)
  end

  test "failure and cooperative deadline cancellation are terminal visible outcomes, never retried" do
    {[{failed, _}], _} = pipeline(16, 1, "fail")
    assert failed["status"] == "failed"
    assert failed["detail"] == "deliberate callback failure"
    {[{cancelled, _}], _} = pipeline(16, 1, "cancel")
    assert cancelled["status"] == "cancelled"
    assert cancelled["detail"] == "application deadline expired"
    assert cancelled["python_ns"] >= 20_000_000
  end

  test "unexpected callback exception stops the consumer without replacement demand or retry" do
    {:ok, producer} = GenStage.start_link(PyBEAM.GenStageProducer, {self(), 16, "unsupported"})
    {:ok, consumer} = GenStage.start(PyBEAM.GenStageConsumer, {self(), 16})
    monitor = Process.monitor(consumer)
    context = GenStage.call(consumer, :context)
    context_monitor = Process.monitor(context)

    try do
      {:ok, _} =
        GenStage.sync_subscribe(consumer,
          to: producer,
          max_demand: 1,
          min_demand: 0,
          cancel: :temporary
        )

      assert_receive {:callback_error, {:error, _}}, 5000
      assert_receive {:DOWN, ^monitor, :process, ^consumer, {:callback_error, _}}, 5000
      assert_receive {:DOWN, ^context_monitor, :process, ^context, :normal}, 5000
      assert_receive :subscription_cancelled, 5000
      stats = GenStage.call(producer, :stats)
      assert stats.emitted == 1
      assert stats.completed == 0
      refute_receive {:outcome, _, _}, 100
    after
      if Process.alive?(consumer), do: GenStage.stop(consumer)
      GenStage.stop(producer)
    end
  end

  test "actual native boundary rejects oversized and invalid payloads without losing the context" do
    {:ok, context} = :py_context.new(%{mode: :worker})

    try do
      base = :json.encode(%{id: 1, action: "fail", text: ""}) |> IO.iodata_to_binary()
      exact = base <> :binary.copy(" ", 1024 - byte_size(base))
      assert {:ok, _} = :py_context.call(context, :genstage_callbacks, :process, [exact])

      assert {:error, _} =
               :py_context.call(context, :genstage_callbacks, :process, [exact <> " "])

      assert {:error, _} = :py_context.call(context, :genstage_callbacks, :process, ["not json"])
      assert {:ok, 2} = :py_context.eval(context, "1 + 1")
    after
      :py_context.stop(context)
    end
  end

  test "killed original coordinator releases its native context without terminate" do
    counters = :py_nif.get_debug_counters()
    baseline = counters.ctx_created - counters.ctx_destroyed

    for _ <- 1..8 do
      {:ok, consumer} = GenStage.start(PyBEAM.GenStageConsumer, {self(), 16})
      context = GenStage.call(consumer, :context)
      monitor = Process.monitor(context)
      Process.exit(consumer, :kill)
      assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000
      after_counters = :py_nif.get_debug_counters()
      assert after_counters.ctx_created - after_counters.ctx_destroyed == baseline
    end
  end

  test "native interrupt is visible and the private context can be reused" do
    {:ok, context} = :py_context.new(%{mode: :worker})
    parent = self()

    :ok =
      :py.register_function(:genstage_interrupt_ready, fn [] ->
        send(parent, :python_ready)
        :ok
      end)

    caller =
      spawn_link(fn ->
        result =
          :py_context.exec(
            context,
            "import erlang\nerlang.call('genstage_interrupt_ready')\nwhile True: pass"
          )

        send(parent, {:interrupted_result, result})
      end)

    try do
      assert_receive :python_ready, 5000
      assert :py_context.interrupt(context) == :ok
      assert_receive {:interrupted_result, {:error, :interrupted}}, 5000
      assert {:ok, 2} = :py_context.eval(context, "1 + 1")
    after
      Process.unlink(caller)
      Process.exit(caller, :kill)
      :py.unregister_function(:genstage_interrupt_ready)
      :py_context.stop(context)
    end
  end

  @tag :benchmark
  test "native identity round trip measures the existing primitive without Python callback loop setup" do
    {:ok, context} = :py_context.new(%{mode: :worker})

    try do
      payload = :binary.copy("x", 1024)

      assert {:ok, ^payload} =
               :py_context.call(context, :genstage_callbacks, :identity, [payload])

      samples =
        Enum.map(1..100, fn _ ->
          started = System.monotonic_time(:nanosecond)

          assert {:ok, ^payload} =
                   :py_context.call(context, :genstage_callbacks, :identity, [payload])

          System.monotonic_time(:nanosecond) - started
        end)
        |> Enum.sort()

      IO.puts(
        "IDENTITY_1024_NS median=#{Enum.at(samples, 50)} p95=#{Enum.at(samples, 94)} samples=100"
      )
    after
      :py_context.stop(context)
    end
  end

  @tag :artifact
  test "Elixir and GenStage assemble and boot as applications in an Erlang host release" do
    applications = [:kernel, :stdlib, :compiler, :elixir, :logger, :gen_stage, :erlang_python]
    versions = Enum.map(applications, &{&1, Application.spec(&1, :vsn)})

    release =
      {:release, {~c"genstage_probe", ~c"0.1.0"}, {:erts, :erlang.system_info(:version)},
       versions}

    directory =
      Path.join(System.tmp_dir!(), "genstage-release-" <> Integer.to_string(System.unique_integer([:positive])))

    File.mkdir!(directory)
    on_exit(fn -> File.rm_rf!(directory) end)
    base = Path.join(directory, "genstage_probe")
    File.write!(base <> ".rel", :io_lib.format(~c"~tp.~n", [release]))
    # Local paths deliberately prove a development host boot, not relocation.
    assert :ok = :systools.make_script(String.to_charlist(base), [:local, :no_warn_sasl])

    assert :ok =
             :systools.make_tar(
               String.to_charlist(base),
               [{:erts, :code.root_dir()}, :no_warn_sasl]
             )

    expression =
      "{ok,C}=py_context:new(\#{mode=>worker}), " <>
        "{ok,[3,14,10]}=py_context:eval(C, <<\"list(__import__('sys').version_info[:3])\">>), " <>
        "ok=py_context:stop(C), " <>
        "io:format(\"RELEASE_OK ~p~n\", [application:get_key(gen_stage, vsn)]), halt()."

    {output, status} =
      System.cmd(
        "erl",
        ["-noshell", "-boot", base, "-erlang_python", "num_contexts", "1", "-eval", expression],
        stderr_to_stdout: true
      )

    assert status == 0, output
    assert output =~ "RELEASE_OK {ok,\"1.3.2\"}"
  end

  @tag :benchmark
  test "native boundary overhead, including copying and JSON parsing, excludes VM startup" do
    {outcomes, _} = pipeline(101, 101, "ok")

    overheads =
      outcomes |> tl() |> Enum.map(fn {result, elapsed} -> elapsed - result["python_ns"] end)

    sorted = Enum.sort(overheads)
    IO.puts("BOUNDARY_NS median=#{Enum.at(sorted, 50)} p95=#{Enum.at(sorted, 94)} samples=100")
    assert Enum.all?(overheads, &(&1 >= 0))
  end
end

result = ExUnit.run()
if result.failures > 0, do: System.halt(1)
