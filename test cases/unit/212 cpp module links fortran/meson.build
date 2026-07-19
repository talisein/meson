project('cpp module links fortran', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# A Fortran library with a C-linkage entry.
flib = static_library('fhelper', 'fhelper.f90')

# A C++ module target that merely links the Fortran library and has no Fortran
# source of its own. process_compilers adds 'fortran' to its compiler dict for
# the link, but the module pipeline follows the C++ TUs: prog gets the full,
# unsplit C++ module pipeline (main.cpp imports mathmod), and the Fortran
# library is just linked in.
prog = executable('prog', 'main.cpp', 'mathmod.cppm', link_with: flib)
test('prog', prog)
