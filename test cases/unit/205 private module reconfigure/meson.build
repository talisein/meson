project('private module reconfigure', 'cpp', default_options: ['cpp_std=c++20'])

# secret is imported only by mylib's own implementation unit (api_impl.cpp),
# never by prog across the link, so it is legal whether declared public or
# private. A build option flips which kwarg carries it, so a reconfigure moves
# the module between public and private without touching anything else. That
# flip must relocate secret's BMI between the shared cache (where linkers of
# mylib can find it) and mylib's own @bmi-private dir (where nothing outside
# mylib can), drop it from mylib's published module set when it goes private,
# and add it back when it returns -- each phase settling to a clean no-op, and
# the round trip leaving the generated build byte-identical.

privacy = get_option('privacy')

if privacy == 'public'
  mylib = static_library('mylib', 'api.cppm', 'secret.cppm', 'api_impl.cpp',
    cpp_module_interfaces: ['api.cppm', 'secret.cppm'])
else
  mylib = static_library('mylib', 'api.cppm', 'secret.cppm', 'api_impl.cpp',
    cpp_module_interfaces: ['api.cppm'],
    cpp_private_module_interfaces: ['secret.cppm'])
endif

prog = executable('prog', 'main.cpp', link_with: mylib)
test('prog', prog)
