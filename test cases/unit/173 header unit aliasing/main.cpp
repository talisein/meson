import "header.hpp";

int cval();
#ifdef WITH_ALIASED
int aval();
#endif

#ifdef FOO
constexpr int WANT = 1;
#else
constexpr int WANT = 0;
#endif

int main() {
    // Constant-evaluated, so this is the value baked in from the BMI this TU
    // compiled against: a BMI from the other class is the wrong answer here.
    constexpr int seen = hv_foo();
    bool ok = seen == WANT && HV == 7 && cval() == 7;
#ifdef WITH_ALIASED
    ok = ok && aval() == 7;
#endif
    return ok ? 0 : 1;
}
