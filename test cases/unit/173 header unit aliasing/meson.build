project('header unit aliasing', 'cpp', default_options: ['cpp_std=c++20'])

# TUs in different directories import the same header unit. With the plain
# include-path spelling every importer resolves the one declared unit. An
# importer-relative spelling ("../header.hpp") resolves to a different
# logical name -- compilers do not normalize it -- so it needs its own
# declared alias unit spelled the way the importer resolves it (mode
# 'aliased'), and without one the import fails at scan (mode 'undeclared').
# Declaring the alias costs no second BMI: the two spellings name one file, so
# they name one BMI. Mode 'classes' declares that aliased unit from two
# divergent BMI classes, which must get one BMI each, reached through either
# spelling.
mode = get_option('mode')

srcs = ['main.cpp', 'foo/consumer.cpp']
units = ['header.hpp']
args = []
if mode != 'plain'
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
