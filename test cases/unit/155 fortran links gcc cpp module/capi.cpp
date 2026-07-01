import mathmod;

// C-linkage entry point that forwards into the named module, so Fortran can
// call the modules-using C++ library through ISO_C_BINDING.
extern "C" int fortran_square(int x) { return square(x); }
