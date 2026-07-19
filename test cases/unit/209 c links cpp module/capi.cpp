import mathmod;

// C-linkage entry point that forwards into the named module, so a C caller can
// reach the modules-using C++ library.
extern "C" int square_entry(int x) { return square(x); }
