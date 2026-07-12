import modlib;

// The BMI this TU imports must have been compiled under this TU's own view
// of FOO -- prog never defines it, modlib does.
#ifdef FOO
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_foo ? (f() - 42) : 1;
}
