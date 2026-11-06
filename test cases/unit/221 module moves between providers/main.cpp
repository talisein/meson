#include "expected.h"

import shadowed;

// Constant-evaluated, so the answer comes from the BMI the compile found,
// never from whichever object the linker picks.
static_assert(shadowed_value == 1 || shadowed_value == 2);

int main() {
    return shadowed_value == EXPECTED ? 0 : 1;
}
