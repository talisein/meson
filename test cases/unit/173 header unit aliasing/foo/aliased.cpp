// Importer-relative spelling: resolves to '<here>/../header.hpp', a logical
// name of its own. Declared as an alias it shares the canonical unit's BMI, so
// this TU reaches its own class's BMI through the alias name.
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
