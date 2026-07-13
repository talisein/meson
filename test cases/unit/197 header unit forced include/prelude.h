#pragma once

// Forced in ahead of every TU, and ahead of the header unit's own compile. It
// says nothing about <vector>, so the probe for that unit must still find it.
#define PRELUDE_TOKEN 1
