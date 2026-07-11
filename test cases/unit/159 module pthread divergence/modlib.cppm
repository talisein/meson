export module modlib;

// -pthread defines _REENTRANT, so the importer can check at run time that
// the BMI it read was compiled under its own POSIX-thread setting.
#ifdef _REENTRANT
export constexpr bool built_with_threads = true;
#else
export constexpr bool built_with_threads = false;
#endif

export int f() { return 42; }
