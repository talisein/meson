project('import std nothreads', 'cpp', default_options: ['cpp_std=c++23'])

std = dependency('std-nothreads')
exe = executable('prog', 'main.cpp', dependencies: std)
test('run', exe)
