project('import std reuse', 'cpp', default_options: ['cpp_std=c++23'])

# Several consumers of dependency('std'): the three in one BMI class must all
# reuse the std target's one BMI, and the one a define splits off gets a
# single BMI-only variant of its own.
std = dependency('std')
foreach n : ['a', 'b', 'c']
  test('prog' + n, executable('prog' + n, 'main.cpp', dependencies: std))
endforeach
test('progdiv', executable('progdiv', 'main.cpp', dependencies: std,
                           cpp_args: ['-DDIVERGENT']))
