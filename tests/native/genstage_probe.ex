defmodule PyBEAM.GenStageProbe do
  alias PyBEAM.GenStage.Bridge

  defp contexts do
    counters = :py_nif.get_debug_counters()
    counters.ctx_created - counters.ctx_destroyed
  end

  defp down(monitor, pid) do
    receive do
      {:DOWN, ^monitor, :process, ^pid, :normal} -> :ok
    after
      3000 -> raise "Owned native context did not drain"
    end
  end

  def verify do
    baseline = contexts()

    graph =
      {_, source, _, _} = Bridge.callback(["open", 16, "genstage_callbacks", "process", "ok"])

    0 = GenStage.call(source, :pulls)

    for sequence <- 0..15 do
      {:ok, payload} = Bridge.callback(["next", graph])
      %{"id" => ^sequence, "detail" => "HÉLLO", "status" => "ok"} = :json.decode(payload)
      pulls = sequence + 1
      ^pulls = GenStage.call(source, :pulls)
    end

    :done = Bridge.callback(["next", graph])
    close(graph, :stage)
    ^baseline = contexts()

    for target <- [:stage, :coordinator], _ <- 1..8 do
      graph =
        {stage, _, context, drain} = Bridge.callback(["open", 16, "genstage_owned", "busy", "ok"])

      monitor = Process.monitor(context)
      Process.exit(if(target == :stage, do: stage, else: drain), :kill)
      down(monitor, context)
      close(graph, target)
      ^baseline = contexts()
    end

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

    try do
      for target <- [:stage, :coordinator] do
        graph =
          {stage, _, context, drain} =
          Bridge.callback(["open", 16, "genstage_owned", "busy", "ok"])

        monitor = Process.monitor(context)

        spawn(fn ->
          try do
            Bridge.callback(["next", graph])
          catch
            :exit, _ -> :ok
          end
        end)

        receive do
          :started -> :ok
        after
          3000 -> raise "Python did not start"
        end

        Process.exit(if(target == :stage, do: stage, else: drain), :kill)

        receive do
          {:DOWN, ^monitor, :process, ^context, _} ->
            raise "Context closed before its running Python work"
        after
          20 -> :ok
        end

        receive do
          :finished -> :ok
        after
          3000 -> raise "Python did not actually finish"
        end

        down(monitor, context)
        close(graph, target)
        ^baseline = contexts()
      end
    after
      :py.unregister_function(:owned_started)
      :py.unregister_function(:owned_finished)
    end

    %{
      "before" => baseline,
      "after" => contexts(),
      "idle" => 16,
      "busy" => 2,
      "ordered" => 16,
      "batches" => 16,
      "max_demand" => 1,
      "initial_pulls" => 0
    }
  end

  defp stopped(pid) do
    monitor = Process.monitor(pid)

    receive do
      {:DOWN, ^monitor, :process, ^pid, _} -> :ok
    after
      3000 -> raise "Graph process remained alive"
    end
  end

  defp close({stage, source, _, drain} = graph, target) do
    acknowledge(graph, target)
    Enum.each([stage, source, drain], &stopped/1)
  end

  defp acknowledge(graph, :stage), do: :ok = Bridge.callback(["close", graph])

  defp acknowledge(graph, :coordinator) do
    try do
      Bridge.callback(["close", graph])
      raise "Dead coordinator falsely acknowledged drain"
    catch
      :exit, _ -> :ok
    end
  end
end
