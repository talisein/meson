project('msvc module define divergence', 'cpp', default_options: ['cpp_std=c++20'])

# modlib is built with -DFOO; prog never defines it. A wrongly shared BMI
# would carry modlib's FOO view, which the constexpr probe turns into a
# wrong exit code.
modlib = static_library('modlib', 'modlib.ixx', cpp_args: ['-DFOO'])
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)
