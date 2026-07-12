export module modlib;

#ifdef _CPPRTTI
export constexpr bool built_with_rtti = true;
#else
export constexpr bool built_with_rtti = false;
#endif

export int f() { return 42; }
