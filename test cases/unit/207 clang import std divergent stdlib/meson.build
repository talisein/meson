project('clang import std divergent stdlib', 'cpp', default_options: ['cpp_std=c++23'])

# The parent uses the platform-default standard library (libstdc++ here) and
# imports std. It gets the bare-named synthesized std library.
std = dependency('std')
prog = executable('prog', 'main.cpp', dependencies: std)
test('prog', prog)

# A subproject that selects libc++ for its own targets and also imports std.
# A different standard library probes different module sources, so it gets its
# OWN synthesized std library (a digest-suffixed name), never shared with the
# parent's libstdc++ one. The two executables are never linked together, so
# mixing the two standard libraries in one build is legitimate.
subproject('cxxlib')
