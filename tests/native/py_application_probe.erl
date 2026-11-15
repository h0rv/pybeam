-module(py_application_probe).
-behaviour(application).
-behaviour(supervisor).
-export([start/2, stop/1, init/1]).

start(_, []) ->
    Helper = code:which(py_application),
    {ok, HelperBinary} = file:read_file(Helper),
    {ok, {py_application, HelperMD5}} = beam_lib:md5(HelperBinary),
    HelperMD5 = py_application:module_info(md5),
    {ok, {py_application, [{compile_info, Compile}]}} = beam_lib:chunks(HelperBinary, [compile_info]),
    "py_application.erl" = filename:basename(proplists:get_value(source, Compile)),
    io:format("LOADER_HELPER compiler=~s md5=~s~n", [proplists:get_value(version, Compile), binary:encode_hex(HelperMD5, lowercase)]),
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
    reject_old_only(Module, Args, Before),
    code:purge(Module),
    false = erlang:check_old_code(Module),
    Dependency = 'Elixir.Logger.Formatter',
    false = code:is_loaded(Dependency),
    false = erlang:check_old_code(Dependency),
    [DependencyFile] = [filename:join(Path, "Elixir.Logger.Formatter.beam") || Path <- Paths,
                       filelib:is_regular(filename:join(Path, "Elixir.Logger.Formatter.beam"))],
    {ok, DependencyBinary} = file:read_file(DependencyFile),
    {module, Dependency} = code:load_binary(Dependency, "old-only-logger-formatter.beam", DependencyBinary),
    true = code:delete(Dependency),
    reject_old_only(Dependency, Args, Before),
    code:purge(Dependency),
    false = code:is_loaded(Dependency),
    false = erlang:check_old_code(Dependency),
    {module, Module} = code:load_binary(Module, File, Expected),
    <<"ok">> = py_application:callback(Args),
    <<"ok">> = py_application:callback(Args),
    ExpectedMD5 = Module:module_info(md5),
    File = code:which(Module),
    supervisor:start_link(?MODULE, []).

reject_old_only(Module, [_, Applications, _] = Args, Before) ->
    false = code:is_loaded(Module),
    true = erlang:check_old_code(Module),
    try py_application:callback(Args), error(old_only_activation_was_accepted)
    catch error:{incompatible_module, Module, unknown_old_code} -> ok end,
    false = code:is_loaded(Module),
    true = erlang:check_old_code(Module),
    Before = code:get_path(),
    lists:foreach(fun({Name, _}) ->
        App = binary_to_existing_atom(Name),
        false = lists:keymember(App, 1, application:which_applications())
    end, Applications).

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
