#pragma once

// Pulls in the header the target declares as a unit. Every TU of the target
// gets <vector> as text before it gets it as a unit -- including the unit's own
// compile, whose main file is that same header.
#include <vector>

#define PRELUDE_TOKEN 1
