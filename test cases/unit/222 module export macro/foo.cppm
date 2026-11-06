module;

#if defined(_WIN32)
#  ifdef foo_EXPORTS
#    define FOO_API __declspec(dllexport)
#  else
#    define FOO_API __declspec(dllimport)
#  endif
#else
#  define FOO_API __attribute__((visibility("default")))
#endif

export module foo;

// Constant-evaluated by the importer: its value is the view of the BMI the
// importer read, which must be foo's own.
#ifdef foo_EXPORTS
export constexpr bool built_with_exports = true;
#else
export constexpr bool built_with_exports = false;
#endif

export FOO_API int m() { return 42; }
