project('std dependency wrap provide', 'cpp', default_options: ['cpp_std=c++23'])

# A plain dependency('std') with no declared fallback: a wrap file carrying
# [provide] std = std_dep supplies the implicit fallback, exactly as it would
# for any other reserved name. Synthesis is the external-lookup candidate and
# wins where available; otherwise the wrap subproject configures and provides
# the dependency.
std = dependency('std')
assert(std.found(), 'std dependency found via neither synthesis nor wrap provide')

executable('prog', 'main.cpp', dependencies: std)
