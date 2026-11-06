project('msvc module define divergence', 'cpp', default_options: ['cpp_std=c++20'])

# modlib is built with -DFOO; prog never defines it. A define does not split
# a named module's BMI class, so prog imports modlib's own BMI and sees its FOO
# view, which the constexpr probe checks at run time.
modlib = static_library('modlib', 'modlib.ixx', cpp_args: ['-DFOO'])
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)
