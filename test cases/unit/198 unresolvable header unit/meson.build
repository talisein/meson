project('unresolvable header unit', 'cpp', default_options: ['cpp_std=c++20'])

# A declared header unit whose header is not on disk while Meson is deciding
# what to build. GCC's mapper server finds a unit by its header's real path, so
# setup must know which file a unit is: a generated one it knows from the
# generator's outputs, but one that resolves nowhere cannot be built at all.
mode = get_option('mode')

if mode == 'generated'
  # The header exists only once the build has run. Setup knows the unit's
  # file from the generator's output, so the unit builds behind its generator
  # and the program runs.
  genh = custom_target('generated.h',
    output: 'generated.h',
    command: [find_program('python3'), files('gen.py'), '@OUTPUT@'])
  prog = executable('prog', 'genmain.cpp', genh, cpp_header_units: ['generated.h'])
  test('prog', prog)
elif mode == 'system-missing'
  # An angle-bracketed spelling that resolves nowhere. Only a probe of the
  # compiler can answer for such a unit, and a probe comes back empty for
  # reasons other than a missing header, so this warns instead of failing setup:
  # the unit is dropped, and a build that does not import it is unharmed. Which
  # means it must also be a clean no-op on the next ninja run -- a unit edge
  # declaring a BMI nothing writes would rebuild itself, and everything ordered
  # behind it, forever.
  prog = executable('prog', 'main.cpp', 'mod.cppm',
                    cpp_header_units: ['<nosuchpkg/nope.h>'])
  test('prog', prog)
elif mode == 'configure'
  # The counterpart, and the line between the two: a header generated at
  # configure time is there to be resolved by the time any of this is decided,
  # so it is a header unit like any other.
  conf = configuration_data()
  conf.set('VALUE', 7)
  configure_file(input: 'configured.h.in', output: 'configured.h',
                 configuration: conf)
  prog = executable('prog', 'confmain.cpp', 'mod.cppm',
                    cpp_header_units: ['configured.h'])
  test('prog', prog)
endif
