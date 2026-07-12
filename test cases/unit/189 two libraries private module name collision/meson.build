project('two libraries private module name collision', 'cpp', default_options: ['cpp_std=c++20'])

# Both libraries export a module literally named "detail" (the module name,
# not the file name, is what could collide).
liba = static_library('liba', 'deta.cppm', 'liba.cpp',
  cpp_private_module_interfaces: ['deta.cppm'])
libb = static_library('libb', 'detb.cppm', 'libb.cpp',
  cpp_private_module_interfaces: ['detb.cppm'])

mode = get_option('mode')

if mode == 'separate'
  # Privacy removes the *global*, whole-build-tree uniqueness requirement:
  # the build tree may contain two same-named private modules as long as
  # they never reach the same link. Each executable links only one library.
  proga = executable('proga', 'main_a.cpp', link_with: liba)
  test('proga', proga)
  progb = executable('progb', 'main_b.cpp', link_with: libb)
  test('progb', progb)
else
  # Both libraries reaching the same link is still a hard error: a module's
  # exported entities are mangled from its bare module name alone (measured:
  # GCC emits the identical symbol, e.g. detail_value@detail(), for both),
  # so two same-named private modules linked together would silently
  # collide at the symbol level -- undefined behavior privacy must not
  # paper over just because neither target ever globally claimed the name.
  prog = executable('prog', 'main.cpp', link_with: [liba, libb])
  test('prog', prog)
endif
