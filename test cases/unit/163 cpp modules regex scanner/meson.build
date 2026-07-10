project('cpp modules regex scanner', 'cpp', default_options: ['cpp_std=c++20'])

# A bare modules flag in cpp_args -- with no module-interface source, no
# cpp_modules kwarg -- routes the target to the legacy regex scanner, the
# pre-P1689 escape hatch. 'flat' must build and run; the other modes use
# constructs the regex scanner cannot model and must fail its scan with a
# clear error. Selected by -Dmode= so one project drives all cases from the
# unit test, which passes the probed flag via -Dmodules_flag=.
mode = get_option('mode')

if mode == 'flat'
  srcs = ['mod.cpp', 'main.cpp']
elif mode == 'partition'
  srcs = ['partition.cpp', 'plainmain.cpp']
elif mode == 'importpart'
  srcs = ['importpart.cpp', 'plainmain.cpp']
else
  srcs = ['importstd.cpp']
endif

prog = executable('prog', srcs, cpp_args: [get_option('modules_flag')])
test('prog', prog)
