project('stdfallback', 'cpp')

# Stand-in for a project shipping the standard library modules some other way.
# A plain declared dependency suffices to prove the fallback was reached and
# returned; the marker version lets a test tell the fallback side apart from
# the compiler-synthesized side.
std_dep = declare_dependency(version: '7.7.7')
