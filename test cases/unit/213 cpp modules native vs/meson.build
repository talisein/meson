project('modtest', 'cpp')

# A named-module executable whose module use is inferred from the .ixx suffix
# alone (no module kwargs). The native vs backend emits it as an ordinary
# compile and MSBuild's own module machinery scans pkg.ixx, produces the BMI,
# and orders main.cpp after it. The Ninja-only module pipeline is not involved.
exe = executable('modtest', 'main.cpp', 'pkg.ixx')
test('modtest', exe)
