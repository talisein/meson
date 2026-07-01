#include "generated.h"   // produced by a custom_target at build time
import mod;              // ...and this TU also imports a module, so it is scanned
int main() { return (gen_val() + mod_v()) == 42 ? 0 : 1; }
