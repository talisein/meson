project('std dependency fallback', 'cpp', default_options: ['cpp_std=c++23'])

# dependency('std') / dependency('std-nothreads') composes with the ordinary
# dependency-fallbacks machinery, like any other reserved name. When the
# compiler can synthesize the standard library modules the synthesis wins and
# the declared fallback is never configured; where synthesis cannot find them
# (no std modules) or is skipped (--force-fallback-for=std), the declared
# fallback subproject provides the dependency instead.
spelling = get_option('spelling')
std = dependency(spelling, fallback: ['stdfallback', 'std_dep'])
assert(std.found(), 'std dependency found via neither synthesis nor fallback')

executable('prog', 'main.cpp', dependencies: std)
