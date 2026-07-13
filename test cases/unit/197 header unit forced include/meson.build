project('header unit forced include', 'cpp', default_options: ['cpp_std=c++20'])

# A forced include and a header unit in one target.
mode = get_option('mode')

if mode == 'ok'
  # The unit is a system header, which no -I of ours accounts for: the backend
  # has to ask the compiler where it resolves, and that probe is the one place
  # a forced include could perturb an answer it has no business changing. The
  # prelude says nothing about <vector>, so the unit must still be found, built
  # and imported.
  prog = executable('prog', 'main.cpp',
                    cpp_args: ['-include', 'prelude.h'],
                    cpp_header_units: ['<vector>'])
  test('prog', prog)
elif mode == 'clash-system'
  # The prelude pulls in the very header declared as a unit. The unit is then
  # compiled from a header whose include guard has already been tripped, so its
  # BMI comes out empty -- importers of it see nothing. This one still links,
  # and only because the same forced include hands every TU the declarations as
  # text; the unit is dead weight. Setup must warn.
  executable('prog', 'main.cpp',
             cpp_args: ['-include', 'clash_prelude.h'],
             cpp_header_units: ['<vector>'])
elif mode == 'clash-user'
  # The same clash on a project-local header, which resolves on the include path
  # with no probe of the compiler needed -- the other half of the diagnostic.
  # Here it is fatal, and the unit's own edge is what dies: the compiler either
  # reads util.h twice (once as the prelude's text, once as the unit itself) and
  # collides with itself, or translates the prelude's #include into an import of
  # the unit it is that moment building. This mode does not build at all.
  executable('prog', 'localmain.cpp',
             cpp_args: ['-include', 'clash_local_prelude.h'],
             cpp_header_units: ['util.h'])
endif
