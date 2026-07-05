project('std dependency override', 'cpp')

# 'std' is a reserved dependency name (the C++ standard library as importable
# modules), but as for every reserved name (openmp, dl, curses, ...) an explicit
# override must win. This needs no import-std-capable compiler: the override
# short-circuits synthesis, so it configures on any C++ toolchain.
placeholder = declare_dependency(version : '9.9.9')
meson.override_dependency('std', placeholder)

std = dependency('std')
assert(std.found())
assert(std.version() == '9.9.9', 'meson.override_dependency(\'std\') was ignored')

executable('prog', 'main.cpp', dependencies : std)
