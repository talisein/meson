project('header unit aliasing', 'cpp', default_options: ['cpp_std=c++20'])

# TUs in different directories import the same header unit. With the plain
# include-path spelling every importer resolves the one declared unit. An
# importer-relative spelling ("../header.hpp") names the same file, so it
# reaches the same unit whether it is declared as a second spelling (mode
# 'aliased') or not (mode 'undeclared'): one file, one BMI. Mode 'classes'
# declares both spellings from two divergent BMI classes, which must get one
# BMI each, reached through either spelling. Mode 'missing' imports a header
# that is declared nowhere, which must fail the build.
mode = get_option('mode')

srcs = ['main.cpp', 'foo/consumer.cpp']
units = ['header.hpp']
args = []
if mode == 'missing'
  srcs += ['foo/missing.cpp']
elif mode != 'plain'
  srcs += ['foo/aliased.cpp']
  args += ['-DWITH_ALIASED']
endif
if mode == 'aliased' or mode == 'classes'
  units += ['foo/../header.hpp']
endif

prog = executable('prog', srcs, cpp_header_units: units, cpp_args: args)
test('prog', prog)

if mode == 'classes'
  progfoo = executable('progfoo', srcs, cpp_header_units: units,
                       cpp_args: args + ['-DFOO'])
  test('progfoo', progfoo)
endif
