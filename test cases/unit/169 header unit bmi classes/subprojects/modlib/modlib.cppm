export module modlib;

import "util.h";

export int mod_util_cpp() {
    constexpr int v = util_cpp();
    return v;
}
