import mathmod;

// A C-linkage entry point that forwards into the C++ named module, callable
// from Fortran through ISO_C_BINDING. This TU imports the module directly --
// the whole point of the one-target shape.
extern "C" int cpp_square(int x) { return square(x); }
