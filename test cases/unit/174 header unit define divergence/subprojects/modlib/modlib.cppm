export module modlib;

import "util.h";

export int mod_util_foo() {
    constexpr int v = util_foo();
    return v;
}
