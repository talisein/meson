import pub;

#if defined _WIN32 || defined __CYGWIN__
  #define DLL_PUBLIC __declspec(dllexport)
#else
  #if defined __GNUC__
    #define DLL_PUBLIC __attribute__ ((visibility("default")))
  #else
    #define DLL_PUBLIC
  #endif
#endif

extern "C" int func_from_app(void);

// Compiling this TU at all requires the executable's public module to be
// published: its BMI must be in the shared class cache and named in the
// executable's provided-modules.json.
static_assert(pub_value() == 20, "resolved the wrong BMI for pub");

extern "C" int DLL_PUBLIC func(void) {
    return pub_value() + func_from_app();
}
