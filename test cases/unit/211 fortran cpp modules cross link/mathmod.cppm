export module mathmod;

// A trivial named-module export, free of any libstdc++ facility so the objects
// link into a Fortran-driven executable without pulling the C++ runtime.
export int square(int x) { return x * x; }
