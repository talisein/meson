// No cpp_header_units here: util_cpp() arrives through modlib's re-export.
// Same BMI class as modlib, so this reads the unit BMI modlib itself built.
import modlib;

int main() {
    constexpr int v = util_cpp();
    return (v == 20 && mod_util_cpp() == 20) ? 0 : 1;
}
