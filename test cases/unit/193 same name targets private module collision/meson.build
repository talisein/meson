project('same name targets private module collision', 'cpp', default_options: ['cpp_std=c++20'])

# sub1 and sub2 both declare a static_library called 'util': a target name is
# only unique within its subdir, so two targets in one build can share one.
# Both privately export a module literally named "detail", so nothing but the
# targets' identities tells the two providers apart.
subdir('sub1')
subdir('sub2')

mode = get_option('mode')

if mode == 'separate'
  # The two same-named targets never reach the same link: privacy removes the
  # global, whole-build-tree name claim, so both "detail" modules coexist.
  prog1 = executable('prog1', 'main1.cpp', link_with: util1)
  test('prog1', prog1)
  prog2 = executable('prog2', 'main2.cpp', link_with: util2)
  test('prog2', prog2)
elif mode == 'collision'
  # Both same-named targets' private "detail" modules reach one link: a hard
  # error, and the diagnostic must tell the two 'util' targets apart.
  executable('prog', 'main.cpp', link_with: [util1, util2])
elif mode == 'distinct'
  # Two private-module providers in one link whose module names differ: no
  # collision, since only the module name is ever mangled into a symbol.
  subdir('sub3')
  prog = executable('prog', 'main_distinct.cpp', link_with: [util1, other])
  test('prog', prog)
else
  # The executable's own private "detail" collides with the one its linked
  # library provides privately: the same symbol-level collision, from the
  # consuming target's own side.
  executable('prog', 'own_main.cpp', 'own_detail.cppm',
    cpp_private_module_interfaces: ['own_detail.cppm'],
    link_with: util1)
endif
