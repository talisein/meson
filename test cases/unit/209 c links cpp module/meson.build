project('c links cpp module', 'c', 'cpp')

# A C++ static library that uses a named module internally: mathmod.cppm is the
# module interface and capi.cpp imports it behind an extern "C" entry point.
# It builds at c++20 through override_options; the project default cpp_std stays
# 'none'.
mathlib = static_library('mathmod', 'mathmod.cppm', 'capi.cpp',
                         override_options: ['cpp_std=c++20'])

# A pure-C program that calls into that modules-using C++ library. It carries a
# cpp compiler only because it links the library (process_compilers adds the
# language to pick the linker), and it compiles no C++ source of its own, so the
# C++-modules cpp_std / scanner checks must not fire on it even at cpp_std=none.
prog = executable('prog', 'main.c', link_with: mathlib)
test('prog', prog)
