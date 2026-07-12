// A plain C++ TU inside the Fortran target. It reaches the modules-using
// library only through the extern "C" boundary: it cannot `import mathmod;`
// itself, because a Fortran target's C++ sources get no module pipeline. That
// is what the warning is about; this shape still builds.
extern "C" int fortran_square(int x);

extern "C" int shim_square(int x) { return fortran_square(x); }
