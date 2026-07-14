project('header unit import inheritance', 'cpp', default_options: ['cpp_std=c++23'])

# modlib is a c++20 module library that declares the header unit "util.h" and
# re-exports it from its interface. Neither program declares that unit: each
# reaches it only by importing modlib, which is the whole point -- a target
# inherits the units its imported interfaces were built against.
#
# cl names a header unit only through a /headerUnit mapping on the command
# line, and an imported .ifc carries none of its own, so an importer that is
# handed no mapping fails the import outright (C7612). It must be given the
# unit's BMI in *its own* BMI class: prog20 shares modlib's, prog23 does not,
# and prog23 must name the c++23 unit its variant of modlib's interface was
# built against rather than the provider's c++20 one.
#
# util_cpp() is constexpr and only ever constant-evaluated, so the dialect each
# program's unit BMI froze is baked into it: a mapping onto the wrong class's
# BMI is a failing run, not merely a build failure.
modlib = subproject('modlib')
modlib_dep = modlib.get_variable('modlib_dep')
prog20 = executable('prog20', 'main20.cpp', dependencies: modlib_dep,
                    override_options: ['cpp_std=c++20'])
prog23 = executable('prog23', 'main23.cpp', dependencies: modlib_dep)
test('prog20', prog20)
test('prog23', prog23)
