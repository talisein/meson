project('gcc module subproject cpp_std divergence', 'cpp',
        default_options: ['cpp_std=c++23'])

# The subproject builds its module library at c++20; the parent consumes it at
# c++23. The divergence crosses the subproject boundary and must be warned.
submod = subproject('submod')
prog = executable('prog', 'main.cpp', dependencies: submod.get_variable('submod_dep'))
test('prog', prog)
