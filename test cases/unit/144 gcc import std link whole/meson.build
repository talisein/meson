project('gcc import std link whole', 'cpp', default_options: ['cpp_std=c++23'])
std = dependency('std')
# Two static libraries that each import std, combined with link_whole. The std
# objects live in a separate std library (not archived into a/b), so
# --whole-archive of these two never double-pulls the std module initializer.
la = static_library('a', 'a.cppm', dependencies: std)
lb = static_library('b', 'b.cppm', dependencies: std)
prog = executable('prog', 'main.cpp', dependencies: std, link_whole: [la, lb])
test('prog', prog)
