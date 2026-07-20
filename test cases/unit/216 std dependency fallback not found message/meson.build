project('std dependency fallback not found message', 'cpp')

# The degraded path: --force-fallback-for=std skips synthesis, and the declared
# fallback cannot supply the dependency (its variable does not exist), so an
# optional dependency('std') returns not-found. When it does, not_found_message
# must fire, exactly as for any other dependency.
std = dependency('std', required: false,
  fallback: ['emptysub', 'missing_var'],
  not_found_message: 'STD MODULES UNAVAILABLE on the degraded path')
assert(not std.found(), 'expected the forced degraded path to yield not-found')
