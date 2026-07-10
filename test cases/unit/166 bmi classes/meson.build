project('bmi classes', 'cpp', default_options: ['cpp_std=c++23'])

# The subproject builds its module library at c++20. prog20 shares that BMI
# class and must reuse the provider's BMI; prog23 diverges and must resolve
# against a synthesized BMI-only variant while still linking the provider's
# objects.
modlib = subproject('modlib')
modlib_dep = modlib.get_variable('modlib_dep')
prog23 = executable('prog23', 'main.cpp', dependencies: modlib_dep)
prog20 = executable('prog20', 'main20.cpp', dependencies: modlib_dep,
                    override_options: ['cpp_std=c++20'])
test('prog23', prog23)
test('prog20', prog20)
