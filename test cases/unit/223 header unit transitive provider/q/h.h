#pragma once

// constexpr, and only ever constant-evaluated by importers, so each TU's value
// comes from the h.h BMI it compiled against.
constexpr int h_view() {
#ifdef FOO
    return 1;
#else
    return 0;
#endif
}
