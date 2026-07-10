project('import std bmi classes', 'cpp', default_options: ['cpp_std=c++23'])

# One dependency('std') provider shared at two dialects: prog23 is in the
# provider's BMI class and reuses its BMIs; prog26 diverges and resolves
# against a BMI-only variant, while both link the same std objects. Nothing
# here is std-specific -- the variant machinery is the generic one.
std = dependency('std')
prog23 = executable('prog23', 'main23.cpp', dependencies: std)
prog26 = executable('prog26', 'main26.cpp', dependencies: std,
                    override_options: ['cpp_std=c++26'])
test('prog23', prog23)
test('prog26', prog26)
