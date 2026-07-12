project('preprocess module interface', 'cpp', default_options: ['cpp_std=c++20'])

cxx = meson.get_compiler('cpp')

# The module interface, used normally: the library provides it, the executable
# imports it across the link.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# The same source, preprocessed. A preprocess-only target neither writes a BMI
# nor imports one and gets no collate edge, so no part of the module pipeline
# may reach its compile: a per-TU module mapper, a scan or a BMI declared here
# would name outputs nothing produces.
pp = cxx.preprocess('modlib.cppm', output: '@PLAINNAME@.ii')

# A preprocess target is never built by default, and ninja only reports a
# dangling dependency for an edge it is actually asked to build -- so put the
# preprocessed output in the default build plan.
custom_target('preprocessed',
  input: pp,
  output: 'modlib.cppm.copy',
  command: [find_program('python3'), '-c',
            'import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2])',
            '@INPUT@', '@OUTPUT@'],
  build_by_default: true)
