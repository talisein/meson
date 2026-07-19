import mathmod;

// A C++ TU in the consumer target that imports the provider's C++ module
// across the link, and exposes a C-linkage entry the Fortran main calls.
extern "C" int cpp_square(int x) { return square(x); }
