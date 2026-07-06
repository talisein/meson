export module usemod;

import std;

// A user module that itself imports std, consumed across a target boundary --
// exercises std alongside ordinary named-module provisioning.
export int mod_value() {
    return static_cast<int>(std::string("twelvechars!").size());
}
