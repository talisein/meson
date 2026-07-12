export module modlib;

#ifdef FOO
export constexpr bool built_with_foo = true;
#else
export constexpr bool built_with_foo = false;
#endif

export int f() { return 42; }
