project('cpp modules pch', 'cpp', default_options: ['cpp_std=c++20'])

# A module provider and its consumer each use a precompiled header. The PCH
# include args ride every compile and scan of a target; a module interface
# unit must not get the forced include -- it would land before the module
# declaration, which is ill-formed -- while ordinary TUs keep it.
modlib = static_library('modlib', 'modlib.cppm', cpp_pch: 'pch/modlib.hh')
prog = executable('prog', 'main.cpp', link_with: modlib, cpp_pch: 'pch/prog.hh')
test('prog', prog)
