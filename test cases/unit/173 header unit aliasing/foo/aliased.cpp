// Importer-relative spelling: resolves to '<here>/../header.hpp', the same file
// as the plain spelling, so this TU reaches its own class's one BMI of it.
import "../header.hpp";

#ifdef FOO
constexpr int WANT = 1;
#else
constexpr int WANT = 0;
#endif

int aval() {
    constexpr int seen = hv_foo();
    return seen == WANT ? HV : -1;
}
