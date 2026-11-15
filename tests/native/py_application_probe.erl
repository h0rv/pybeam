-module(py_application_probe).
-behaviour(application).
-behaviour(supervisor).
-export([start/2, stop/1, init/1]).

start(_, []) ->
    NativeApp = code:lib_dir(erlang_python),
    Root = filename:join([filename:dirname(filename:dirname(NativeApp)), "optional", "genstage"]),
    Paths = filelib:wildcard(filename:join([Root, "lib", "*", "ebin"])),
    Applications = [metadata(Path) || Path <- Paths],
    Module = 'Elixir.GenStage',
    [File] = [filename:join(Path, "Elixir.GenStage.beam") || Path <- Paths,
             filelib:is_regular(filename:join(Path, "Elixir.GenStage.beam"))],
    {ok, Expected} = file:read_file(File),
    {ok, {Module, ExpectedMD5}} = beam_lib:md5(Expected),
    Probe = filename:dirname(filename:dirname(code:which(?MODULE))),
    {ok, Stale} = file:read_file(filename:join([Probe, "priv", "stale.beam"])),
    {module, Module} = code:load_binary(Module, "stale-genstage.beam", Stale),
    ActualMD5 = Module:module_info(md5),
    Before = code:get_path(),
    Args = [[list_to_binary(Path) || Path <- Paths], Applications,
            list_to_binary(erlang:system_info(version))],
    try py_application:callback(Args), error(stale_activation_was_accepted)
    catch error:{incompatible_module, Module, ExpectedMD5, ActualMD5} -> ok end,
    old = Module:stale_marker(),
    ActualMD5 = Module:module_info(md5),
    "stale-genstage.beam" = code:which(Module),
    Before = code:get_path(),
    false = lists:keymember(gen_stage, 1, application:which_applications()),
    true = code:delete(Module),
    code:purge(Module),
    {module, Module} = code:load_binary(Module, File, Expected),
    <<"ok">> = py_application:callback(Args),
    <<"ok">> = py_application:callback(Args),
    ExpectedMD5 = Module:module_info(md5),
    File = code:which(Module),
    supervisor:start_link(?MODULE, []).

metadata(Path) ->
    [File] = filelib:wildcard(filename:join(Path, "*.app")),
    {ok, [{application, Name, Properties} = Spec]} = file:consult(File),
    case application:load(Spec) of
        ok -> ok;
        {error, {already_loaded, _}} -> ok
    end,
    Version = proplists:get_value(vsn, Properties),
    {ok, Version} = application:get_key(Name, vsn),
    {atom_to_binary(Name), list_to_binary(Version)}.

stop(_) -> ok.
init([]) -> {ok, {#{strategy => one_for_one, intensity => 0, period => 1}, []}}.
