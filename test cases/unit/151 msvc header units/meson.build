project('msvc header units', 'cpp', default_options: ['cpp_std=c++latest'])

# A user header unit ("util.h"), a system header unit ("<angleutil.h>") and a
# named module ("mod") in one target. cl has no directory lookup for header
# units, so Meson pre-builds each unit's .ifc and passes an explicit
# /headerUnit:<spelling>=<ifc> mapping to the consumer -- while the named module
# still rides the scan/collate/dyndep path unchanged.
prog = executable('prog', 'main.cpp', 'mod.ixx',
                  cpp_header_units: ['util.h', '<angleutil.h>'])
test('prog', prog)

# A second target importing the same user unit. The unit build edge is deduped
# globally by (mode, spelling), so both targets share one .ifc.
prog2 = executable('prog2', 'main2.cpp',
                   cpp_header_units: ['util.h'])
test('prog2', prog2)
