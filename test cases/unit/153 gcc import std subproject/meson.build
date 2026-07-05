project('gcc import std subproject', 'cpp', default_options: ['cpp_std=c++23'])

# The parent project uses `import std` directly.
std = dependency('std')

# A subproject that *also* calls dependency('std'). dependency('std') is
# memoized per Interpreter, and every subproject gets its own Interpreter, so
# each subproject that asks for std used to synthesize its own
# static_library('std'). When such a subproject library is linked into a parent
# target that also uses std, two distinct targets both provide module 'std' and
# both reach the same executable -- a link-time "Module 'std' is provided by
# more than one target" collision. The std library must be synthesized once for
# the whole build instead.
sub = subproject('substd')
submod = sub.get_variable('submod')

# prog links the std library directly (import std) *and* the subproject module,
# which imports std transitively. Before the fix this reached two std providers.
prog = executable('prog', 'main.cpp', dependencies: std, link_with: submod)
test('prog', prog)
