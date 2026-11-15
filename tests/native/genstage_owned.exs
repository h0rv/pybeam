ExUnit.start(autorun: false)

defmodule PyBEAM.GenStage.OwnedTest do
  use ExUnit.Case, async: false
  alias PyBEAM.GenStage.Bridge

  defp contexts do
    counters = :py_nif.get_debug_counters()
    counters.ctx_created - counters.ctx_destroyed
  end

  defp open(function) do
    Bridge.callback(["open", 16, "genstage_owned", function, "ok"])
  end

  test "loaded versions, successful transform, ordering, finite batches and demand" do
    assert Application.spec(:gen_stage, :vsn) == ~c"1.3.2"
    assert System.version() == "1.20.0"

    assert File.read!(Path.join([:code.root_dir(), "releases", "29", "OTP_VERSION"]))
           |> String.trim() == "29.1.2"

    baseline = contexts()

    graph =
      {stage, source, context, _} =
      Bridge.callback(["open", 16, "examples.genstage_callbacks", "process", "ok"])

    assert {:ok, [3, 14, 10]} =
             :py_context.eval(context, "list(__import__('sys').version_info[:3])")

    assert GenStage.call(source, :pulls) == 0

    for n <- 0..15 do
      assert {:ok, payload} = Bridge.callback(["next", graph])
      assert %{"id" => ^n, "detail" => "HÉLLO", "status" => "ok"} = :json.decode(payload)
      assert GenStage.call(source, :pulls) == n + 1
    end

    assert :done = Bridge.callback(["next", graph])
    assert :ok = Bridge.callback(["close", graph])
    refute Process.alive?(stage)
    assert contexts() == baseline
  end

  test "kill idle stage repeatedly drains its actual native context" do
    baseline = contexts()

    for _ <- 1..8 do
      graph = {stage, source, context, _} = open("busy")
      monitor = Process.monitor(context)
      assert GenStage.call(source, :pulls) == 0
      Process.exit(stage, :kill)
      assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000
      assert :ok = Bridge.callback(["close", graph])
      assert contexts() == baseline
    end
  end

  test "busy kill permits an effect after PID death but waits for native drain" do
    baseline = contexts()
    owner = self()

    :ok =
      :py.register_function(:owned_started, fn [] ->
        send(owner, :started)
        :ok
      end)

    :ok =
      :py.register_function(:owned_finished, fn [] ->
        send(owner, :finished)
        :ok
      end)

    for target <- [:stage, :coordinator] do
      graph = {stage, _, context, drain} = open("busy")
      monitor = Process.monitor(context)

      spawn(fn ->
        try do
          Bridge.callback(["next", graph])
        catch
          :exit, _ -> :ok
        end
      end)

      assert_receive :started, 3000
      Process.exit(if(target == :stage, do: stage, else: drain), :kill)
      refute_receive {:DOWN, ^monitor, :process, ^context, _}, 20
      assert_receive :finished, 3000
      assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000

      if target == :stage do
        assert :ok = Bridge.callback(["close", graph])
      else
        assert catch_exit(Bridge.callback(["close", graph]))
      end

      assert contexts() == baseline
    end

    :py.unregister_function(:owned_started)
    :py.unregister_function(:owned_finished)
  end

  test "killed drain coordinator fails closed and still releases stage-owned contexts" do
    baseline = contexts()

    for _ <- 1..8 do
      graph = {stage, source, context, drain} = open("busy")
      monitor = Process.monitor(context)
      stage_monitor = Process.monitor(stage)
      source_monitor = Process.monitor(source)
      Process.exit(drain, :kill)
      assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000
      assert_receive {:DOWN, ^stage_monitor, :process, ^stage, :normal}, 3000
      assert_receive {:DOWN, ^source_monitor, :process, ^source, :normal}, 3000
      assert catch_exit(Bridge.callback(["close", graph]))
      assert contexts() == baseline
    end
  end

  test "source death closes the consumer context without issuing replacement demand" do
    baseline = contexts()
    graph = {_, source, context, _} = open("busy")
    monitor = Process.monitor(context)
    Process.exit(source, :kill)
    assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000
    assert :ok = Bridge.callback(["close", graph])
    assert contexts() == baseline
  end

  test "raised RuntimeError is visible, no retry and context count restored" do
    baseline = contexts()
    graph = {_, _, context, _} = open("raises")
    monitor = Process.monitor(context)
    assert {:error, {:error, {:RuntimeError, _}}} = Bridge.callback(["next", graph])
    assert_receive {:DOWN, ^monitor, :process, ^context, :normal}, 3000
    assert :ok = Bridge.callback(["close", graph])
    assert contexts() == baseline
  end

  test "consumer break and explicit graph shutdown do not pull more work" do
    baseline = contexts()

    for _ <- 1..8 do
      graph =
        {_, source, _, _} =
        Bridge.callback(["open", 16, "examples.genstage_callbacks", "process", "ok"])

      assert {:ok, _} = Bridge.callback(["next", graph])
      assert GenStage.call(source, :pulls) == 1
      assert :ok = Bridge.callback(["close", graph])
      assert contexts() == baseline
    end
  end
end

result = ExUnit.run()
if result.failures > 0, do: System.halt(1)
