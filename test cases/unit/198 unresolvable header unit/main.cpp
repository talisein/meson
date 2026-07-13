// The declared header unit is deliberately not imported here: it resolves
// nowhere, so Meson builds no BMI for it, and this target must still build,
// link and run.
import util;

int main() {
    return util_val() == 3 ? 0 : 1;
}
