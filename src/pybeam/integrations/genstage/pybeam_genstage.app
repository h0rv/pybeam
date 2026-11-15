{application, pybeam_genstage, [
    {vsn, "0.1.0"},
    {description, "Optional GenStage Python callbacks"},
    {modules, [pybeam_genstage, 'Elixir.PyBEAM.GenStage.Source',
               'Elixir.PyBEAM.GenStage.Stage', 'Elixir.PyBEAM.GenStage.Drain',
               'Elixir.PyBEAM.GenStage.Bridge']},
    {registered, []},
    {applications, [kernel, stdlib, compiler, elixir, logger, gen_stage, erlang_python]},
    {mod, {pybeam_genstage, []}}
]}.
