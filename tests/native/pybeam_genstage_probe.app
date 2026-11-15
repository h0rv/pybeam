{application, pybeam_genstage_probe, [
    {vsn, "0.1.0"},
    {description, "Installed optional application regression fixture"},
    {modules, [pybeam_genstage_probe, 'Elixir.PyBEAM.GenStageProbe']},
    {registered, []},
    {applications, [kernel, stdlib, pybeam_genstage]},
    {mod, {pybeam_genstage_probe, []}}
]}.
