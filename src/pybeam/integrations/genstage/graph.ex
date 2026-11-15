defmodule PyBEAM.GenStage.Source do
  use GenStage

  def init({count, action}) do
    source = Stream.map(0..(count - 1), &%{id: &1, action: action, text: "héllo"})
    {:producer, %{source: source, continuation: nil, pulls: 0}, buffer_size: 1}
  end

  def handle_demand(1, state) do
    reducer = fn item, _ -> {:suspend, item} end

    result =
      if state.continuation,
        do: state.continuation.({:cont, nil}),
        else: Enumerable.reduce(state.source, {:cont, nil}, reducer)

    case result do
      {:suspended, item, continuation} ->
        payload = :json.encode(item) |> IO.iodata_to_binary()
        true = byte_size(payload) <= 1024
        {:noreply, [payload], %{state | continuation: continuation, pulls: state.pulls + 1}}

      _ ->
        {:noreply, [], state}
    end
  end

  def handle_call(:pulls, _from, state), do: {:reply, state.pulls, [], state}
  def handle_cancel(_, _, state), do: {:stop, :normal, state}
  def handle_info(:close, state), do: {:stop, :normal, state}

  def terminate(_, %{continuation: continuation}) when is_function(continuation),
    do: continuation.({:halt, nil})

  def terminate(_, _), do: :ok
end

defmodule PyBEAM.GenStage.Stage do
  use GenStage

  def init({source, count, module, function, action}) do
    {:ok, context} = :py_context.new(%{mode: :worker, context_owner: self()})
    owner = :py.context(1)
    monitor = Process.monitor(owner)

    {:consumer,
     %{
       source: source,
       context: context,
       count: count,
       module: module,
       function: function,
       action: action,
       completed: 0,
       from: nil,
       waiter: nil,
       owner: monitor
     }}
  end

  def handle_subscribe(:producer, _, from, state), do: {:manual, %{state | from: from}}

  def handle_call(:next, _caller, %{completed: n, count: n} = state),
    do: {:reply, :done, [], state}

  def handle_call(:next, caller, %{waiter: nil} = state) do
    GenStage.ask(state.from, 1)
    {:noreply, [], %{state | waiter: caller}}
  end

  def handle_call(:context, _, state), do: {:reply, state.context, [], state}

  def handle_call({:owner, owner}, _, state) do
    Process.demonitor(state.owner, [:flush])
    {:reply, :ok, [], %{state | owner: Process.monitor(owner)}}
  end

  def handle_events([payload], _, state) do
    result = :py_context.call(state.context, state.module, state.function, [payload], %{}, 2000)

    case result do
      {:ok, response} when is_binary(response) and byte_size(response) <= 1024 ->
        GenStage.reply(state.waiter, {:ok, response})
        {:noreply, [], %{state | completed: state.completed + 1, waiter: nil}}

      error ->
        GenStage.reply(state.waiter, {:error, error})
        {:stop, {:callback_error, error}, state}
    end
  end

  def handle_info({:DOWN, monitor, :process, _, _}, %{owner: monitor} = state),
    do: {:stop, :normal, state}

  def handle_info(:close, state), do: {:stop, :normal, state}
  def handle_cancel(reason, _, state), do: {:stop, reason, state}
  def terminate(_, state), do: :py_context.stop(state.context)
end

defmodule PyBEAM.GenStage.Drain do
  use GenServer

  def init({stage, source, context, owner}) do
    monitors =
      Map.new([{stage, :stage}, {source, :source}, {context, :context}], fn {pid, name} ->
        {Process.monitor(pid), name}
      end)

    {:ok,
     %{
       stage: stage,
       source: source,
       monitors: monitors,
       owner: Process.monitor(owner),
       context_reason: nil,
       closing: false,
       waiter: nil
     }}
  end

  def handle_call(:close, caller, state) do
    send(state.stage, :close)
    send(state.source, :close)
    finish(%{state | closing: true, waiter: caller})
  end

  def handle_info({:DOWN, monitor, :process, _, _}, %{owner: monitor} = state) do
    send(state.stage, :close)
    send(state.source, :close)
    finish(%{state | closing: true})
  end

  def handle_info({:DOWN, monitor, :process, _, reason}, state) do
    {name, monitors} = Map.pop!(state.monitors, monitor)
    send(state.stage, :close)
    send(state.source, :close)
    reason = if name == :context, do: reason, else: state.context_reason
    finish(%{state | monitors: monitors, context_reason: reason})
  end

  defp finish(%{closing: true, monitors: monitors} = state) when map_size(monitors) == 0 do
    if state.waiter do
      result =
        if state.context_reason == :normal,
          do: :ok,
          else: {:error, {:unacknowledged_context_drain, state.context_reason}}

      GenServer.reply(state.waiter, result)
    end

    {:stop, :normal, state}
  end

  defp finish(state), do: {:noreply, state}
end

defmodule PyBEAM.GenStage.Bridge do
  def install do
    :ok = :py.register_function(:pybeam_genstage, &callback/1)
  end

  def callback([<<"open">>, count, module, function, action]) when count > 0 and count <= 16 do
    {:ok, source} = GenStage.start(PyBEAM.GenStage.Source, {count, action})

    case GenStage.start(PyBEAM.GenStage.Stage, {source, count, module, function, action}) do
      {:ok, stage} ->
        context = GenStage.call(stage, :context)

        {:ok, drain} =
          GenServer.start(PyBEAM.GenStage.Drain, {stage, source, context, :py.context(1)})

        :ok = GenStage.call(stage, {:owner, drain})

        {:ok, _} =
          GenStage.sync_subscribe(stage,
            to: source,
            max_demand: 1,
            min_demand: 0,
            cancel: :temporary
          )

        {stage, source, context, drain}

      error ->
        GenStage.stop(source)
        error
    end
  end

  def callback([<<"contexts">>]) do
    counters = :py_nif.get_debug_counters()
    counters.ctx_created - counters.ctx_destroyed
  end

  def callback([<<"next">>, {stage, _, _, _}]), do: GenStage.call(stage, :next, 3000)
  def callback([<<"close">>, {_, _, _, drain}]), do: GenServer.call(drain, :close, 3000)
end
