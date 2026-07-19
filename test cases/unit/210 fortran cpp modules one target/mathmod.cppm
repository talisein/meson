export module mathmod;

// A trivial named-module export. Kept free of any libstdc++ facility so the
// resulting objects link into a Fortran-driven executable without pulling the
// C++ runtime.
export int square(int x) { return x * x; }
