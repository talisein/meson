export module modlib;

// Re-exported, so an importer of modlib sees util_cpp() without importing the
// unit itself -- and so the importer's compiler has to be able to name the
// unit's BMI while reading this interface.
export import "util.h";

export int mod_util_cpp() {
    constexpr int v = util_cpp();
    return v;
}
