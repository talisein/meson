import modlib;

// The BMI this TU imports must have been compiled under this TU's own view
// of _CPPUNWIND -- prog is cpp_eh=none, modlib is the default (/EHsc).
#ifdef _CPPUNWIND
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_eh ? (f() - 42) : 1;
}
