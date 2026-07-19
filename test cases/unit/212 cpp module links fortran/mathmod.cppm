export module mathmod;

// A trivial named-module export, free of any libstdc++ facility so the objects
// link alongside Fortran objects without pulling the C++ runtime.
export int square(int x) { return x * x; }
