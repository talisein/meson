export module modlib;

// The importer constant-evaluates these, so their values come from whichever
// BMI the importer read: a wrong BMI is a wrong answer at run time, not just a
// build hazard.
#ifdef FOO
export constexpr bool built_with_foo = true;
#else
export constexpr bool built_with_foo = false;
#endif
export constexpr long built_std = __cplusplus;

export int f() { return 42; }
