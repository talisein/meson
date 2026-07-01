project('gcc cpp modules generated header', 'cpp', default_options: ['cpp_std=c++20'])
# A generated header a module TU includes must exist before that TU is *scanned*,
# not just before it is compiled -- the scan preprocesses the source too.
genh = custom_target('generated.h',
  output: 'generated.h',
  command: [find_program('python3'), files('gen.py'), '@OUTPUT@'])
prog = executable('prog', 'main.cpp', 'mod.cppm', genh)
test('prog', prog)
