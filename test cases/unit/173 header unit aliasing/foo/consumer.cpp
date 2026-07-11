// Same spelling as main.cpp, from another directory: resolves through the
// include path to the same logical name, so the one unit BMI serves both.
import "header.hpp";

int cval() { return HV; }
