project('stl header units', 'cpp', default_options: ['cpp_std=c++20'])

# A real standard-library header as a header unit: <vector> resolves to an
# absolute system path, unlike the project-local units of the other fixtures.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib,
                  cpp_header_units: ['<vector>'])
test('prog', prog)

# A define-divergent second consumer makes the build multi-class, so prog's
# TUs resolve modules and the header unit per class -- on GCC the unit keyed by
# its absolute real path.
progfoo = executable('progfoo', 'foomain.cpp', link_with: modlib,
                     cpp_args: ['-DFOO'])
test('progfoo', progfoo)
