project('module bmi flag divergence', 'cpp', default_options: ['cpp_std=c++20'])

# modlib and prog always diverge in optimization (BMI-irrelevant, one BMI
# class). -Dmode=define adds a -D divergence, which a named module's BMI class
# ignores: prog imports modlib's own BMI, preprocessed under modlib's FOO.
# -Dmode=std adds a dialect divergence, which splits the BMI class: prog
# imports a BMI-only variant built at its own cpp_std.
mode = get_option('mode')
modlib = static_library('modlib', 'modlib.cppm',
                        cpp_args: mode == 'define' ? ['-DFOO'] : [],
                        override_options: mode == 'std' ? ['cpp_std=c++23'] : [])
prog = executable('prog', 'main.cpp', link_with: modlib,
                  cpp_args: mode == 'define' ? ['-DEXPECT_FOO'] : [],
                  override_options: ['optimization=3'])
test('prog', prog)
