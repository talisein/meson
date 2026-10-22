project('module bmi flag divergence', 'cpp', default_options: ['cpp_std=c++20'])

# modlib and prog always diverge in optimization (BMI-irrelevant, one BMI
# class); -Dwithfoo=true adds a -D divergence (BMI-affecting, two classes).
modlib = static_library('modlib', 'modlib.cppm',
                        cpp_args: get_option('withfoo') ? ['-DFOO'] : [])
prog = executable('prog', 'main.cpp', link_with: modlib,
                  override_options: ['optimization=3'])
test('prog', prog)
