import modlib;

// prog never defines FOO; modlib does. A named interface is preprocessed once,
// under its provider's flags, so prog must see modlib's view: a BMI-only
// variant recompiled without FOO would describe code modlib's object never
// contained.
int main() {
    return built_with_foo ? (f() - 42) : 1;
}
