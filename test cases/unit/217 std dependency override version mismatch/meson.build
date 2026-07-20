project('std dependency override version mismatch', 'cpp')

# An explicit override registers under the same identifier the reserved 'std'
# lookup uses, so it is consulted at stage 1 of the fallbacks ladder. A
# version: constraint the override cannot satisfy makes stage 1 return
# not-found and stops the ladder before synthesis, so a required dependency
# hard-fails rather than silently falling through to the compiler.
placeholder = declare_dependency(version: '1.0')
meson.override_dependency('std', placeholder)

std = dependency('std', version: '>=2.0')
