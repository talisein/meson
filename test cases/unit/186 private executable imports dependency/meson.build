project('private executable imports dependency', 'cpp', default_options: ['cpp_std=c++20'])

# An executable's own module is private (its own BMI cache directory), but it
# can still link a library and import that library's public module from the
# shared class cache -- the two resolutions must not interfere. This is the
# primary scenario the private BMI directory has to support, not an edge
# case: a test executable exporting scaffolding of its own while importing
# the library under test.
libmod = static_library('libmod', 'libmod.cppm')
test1 = executable('test1', 'test1.cppm', 'main1.cpp', link_with: libmod)
test('test1', test1)
