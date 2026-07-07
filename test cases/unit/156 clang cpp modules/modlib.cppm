module;
// A real system include in the global module fragment: the scan preprocesses
// it, so this covers clang-scan-deps' resource-directory resolution (builtin
// headers like float.h are reached through <cfloat>).
#include <cfloat>
export module modlib;

export int modfunc() {
    static_assert(DBL_DIG > 0, "include was preprocessed");
    return 42;
}
