project('cpp modules opt out', 'cpp', default_options: ['cpp_std=c++17'])

# cpp_modules is a tristate. Unset, a .cppm/.ixx is inferred a module
# interface (and needs cpp_std c++20); an explicit value overrides that
# inference either way. These three modes exercise all three states under a
# single sub-c++20 dialect.
mode = get_option('mode')

if mode == 'optout'
  # false opts the target out of module treatment: the .ixx compiles as
  # ordinary C++ with no cpp_std c++20 floor.
  prog = executable('prog', 'iface.ixx', cpp_modules: false)
  test('run', prog)
elif mode == 'inferred'
  # Unset: the .ixx is inferred a module interface and the c++20 floor fires
  # at setup. This is the regression guard for the opt-out above.
  executable('prog', 'iface.ixx')
elif mode == 'forced'
  # true forces module treatment onto a plain .cpp the extension would never
  # infer, so the c++20 floor fires here too.
  executable('prog', 'plain.cpp', cpp_modules: true)
endif
