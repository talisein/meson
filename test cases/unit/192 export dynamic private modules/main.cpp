import pub;
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

// The symbol the plugin resolves in the executable -- the whole point of
// export_dynamic, and on PE/COFF what makes the executable's import library
// exist for the plugin to link against.
extern "C" int DLL_PUBLIC func_from_app(void) {
    return 1000;
}

int main() {
    // Both of the executable's own modules resolve in one target: priv from
    // its private BMI directory, pub from the shared class cache.
    return pub_value() + priv_value() == 42 ? 0 : 1;
}
