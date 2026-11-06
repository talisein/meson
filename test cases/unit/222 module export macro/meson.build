project('module export macro', 'cpp', default_options: ['cpp_std=c++20'])

# foo's interface picks dllexport or dllimport with an export macro that only
# foo's own compile defines, as generated export headers do. prog does not
# define it, and must import foo's own BMI: a named interface is preprocessed
# once, under its provider's flags. A BMI-only variant recompiled without the
# macro would define a dllimport function (rejected by cl, C2491), and describe
# an interface foo's object does not have.
foo = shared_library('foo', 'foo.cppm', cpp_args: ['-Dfoo_EXPORTS'])
prog = executable('prog', 'main.cpp', link_with: foo)
test('prog', prog)
