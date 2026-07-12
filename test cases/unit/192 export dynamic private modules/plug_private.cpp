import priv;

#if defined _WIN32 || defined __CYGWIN__
  #define DLL_PUBLIC __declspec(dllexport)
#else
  #if defined __GNUC__
    #define DLL_PUBLIC __attribute__ ((visibility("default")))
  #else
    #define DLL_PUBLIC
  #endif
#endif

// Importing the executable's *private* module from outside it: rejected at
// collate time, with the module and its providing target named.
extern "C" int DLL_PUBLIC func(void) {
    return priv_value();
}
