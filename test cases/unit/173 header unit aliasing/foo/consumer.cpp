// Same spelling as main.cpp, from another directory: resolves through the
// include path to the same logical name, so the one unit BMI serves both.
import "header.hpp";

#ifdef FOO
constexpr int WANT = 1;
#else
constexpr int WANT = 0;
#endif

int cval() {
    constexpr int seen = hv_foo();
    return seen == WANT ? HV : -1;
}
