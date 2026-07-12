export module modlib;

#ifdef _CPPUNWIND
export constexpr bool built_with_eh = true;
#else
export constexpr bool built_with_eh = false;
#endif

export int f() { return 42; }
