export module submod;

import std;

// A subproject module that itself imports std, consumed across the subproject
// boundary by the parent executable.
export int sub_value() {
    return static_cast<int>(std::string("subproject!!").size());
}
