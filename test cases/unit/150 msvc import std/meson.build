project('msvc import std', 'cpp', default_options: ['cpp_std=c++latest'])

# The standard library as importable modules. This both provides `import std;`
# and module-enables the targets that use it, so no cpp_modules kwarg is needed.
std = dependency('std')

# `import std;`
prog = executable('prog', 'main.cpp', dependencies: std)
test('prog', prog)

# `import std.compat;` (which imports std) is provided the same way.
compatprog = executable('compatprog', 'compatmain.cpp', dependencies: std)
test('compatprog', compatprog)

# A user module that imports std, provided by a library and consumed across a
# target boundary: usemain only links usemod and never imports std itself, yet
# still links the std objects transitively without declaring dependency('std').
usemodlib = static_library('usemod', 'usemod.ixx', dependencies: std)
usemain = executable('usemain', 'usemain.cpp', link_with: usemodlib)
test('usemain', usemain)
