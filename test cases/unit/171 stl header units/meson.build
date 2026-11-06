project('stl header units', 'cpp', default_options: ['cpp_std=c++20'])

# A real standard-library header as a header unit: <vector> resolves to an
# absolute system path, unlike the project-local units of the other fixtures.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib,
                  cpp_header_units: ['<vector>'])
test('prog', prog)

# A define-divergent second consumer. modlib declares no header units, so the
# define does not split its named-module BMI class -- both programs import its
# one BMI -- but it does split header units, so prog's TUs resolve the unit in
# a named header-unit BMI class -- on GCC the unit keyed by its absolute real
# path.
progfoo = executable('progfoo', 'foomain.cpp', link_with: modlib,
                     cpp_args: ['-DFOO'])
test('progfoo', progfoo)
