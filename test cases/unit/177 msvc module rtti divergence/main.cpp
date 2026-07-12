import modlib;

// The BMI this TU imports must have been compiled under this TU's own view
// of _CPPRTTI -- prog is /GR-, modlib is not.
#ifdef _CPPRTTI
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_rtti ? (f() - 42) : 1;
}
