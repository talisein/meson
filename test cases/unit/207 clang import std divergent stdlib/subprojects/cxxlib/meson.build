project('cxxlib', 'cpp', default_options: ['cpp_std=c++23'])

# This subproject builds against libc++, not the platform-default stdlib.
add_project_arguments('-stdlib=libc++', language: 'cpp')
add_project_link_arguments('-stdlib=libc++', language: 'cpp')

# Its own dependency('std') probes libc++'s module manifest, so it synthesizes a
# std library distinct from the parent's.
std = dependency('std')
cxxprog = executable('cxxprog', 'main.cpp', dependencies: std)
test('cxxprog', cxxprog)
