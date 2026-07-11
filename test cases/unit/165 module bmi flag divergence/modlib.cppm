export module modlib;

// The importer constant-evaluates this, so its value comes from whichever
// BMI the importer read: a divergent shared BMI is a wrong answer at run
// time, not just a build hazard.
#ifdef FOO
export constexpr bool built_with_foo = true;
#else
export constexpr bool built_with_foo = false;
#endif

export int f() { return 42; }
