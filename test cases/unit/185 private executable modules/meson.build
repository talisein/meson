project('private executable modules', 'cpp', default_options: ['cpp_std=c++20'])

# Nothing can ever link an executable, so its modules are private to it by
# construction: two executables exporting the same module name must not
# collide, even though a shared build-wide BMI cache would otherwise force
# them to fight over one cache address.
test1 = executable('test1', 'test1.cppm', 'main1.cpp')
test2 = executable('test2', 'test2.cppm', 'main2.cpp')
test('test1', test1)
test('test2', test2)
