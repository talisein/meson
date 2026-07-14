// A divergent class: modlib's interface is re-BMI'd here, against a c++23
// util.h unit, so the inherited util_cpp() reports 23 -- while mod_util_cpp(),
// which lives in modlib's objects, is still the one c++20 compile.
import modlib;

int main() {
    constexpr int v = util_cpp();
    return (v == 23 && mod_util_cpp() == 20) ? 0 : 1;
}
