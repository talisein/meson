import mathmod;

// A C++ TU inside a mixed Fortran/C++ target that links the module provider.
// It imports the provider's named module directly across the link and exposes
// a C-linkage entry the Fortran main calls.
extern "C" int fortran_square(int x) { return square(x); }
