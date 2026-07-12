export module modlib;

#ifdef __cpp_exceptions
export constexpr bool built_with_eh = true;
#else
export constexpr bool built_with_eh = false;
#endif

#ifdef __cpp_rtti
export constexpr bool built_with_rtti = true;
#else
export constexpr bool built_with_rtti = false;
#endif

export int f() { return 42; }
