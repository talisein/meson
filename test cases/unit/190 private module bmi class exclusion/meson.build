project('private module bmi class exclusion', 'cpp', default_options: ['cpp_std=c++20'])

# pubmod is public and crosses a BMI class boundary (prog23 below); priv is
# private and imported only from pubmod's implementation unit, never its
# interface, so it never needs to be resolved under a foreign class's flags.
mylib = static_library('mylib', 'pubmod.cppm', 'priv.cppm', 'pubmod_impl.cpp',
  cpp_module_interfaces: ['pubmod.cppm'],
  cpp_private_module_interfaces: ['priv.cppm'])

# Same-class consumer: resolves mylib's own BMI directly.
prog = executable('prog', 'main.cpp', link_with: mylib)
test('prog', prog)

# Divergent-class consumer: forces a BMI-only variant of pubmod under a
# diverging dialect. The variant must recompile only pubmod's public
# interface, never priv.
prog23 = executable('prog23', 'main.cpp', link_with: mylib,
                    override_options: ['cpp_std=' + get_option('divergent_std')])
test('prog23', prog23)
