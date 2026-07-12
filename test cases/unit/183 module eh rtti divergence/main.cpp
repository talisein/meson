import modlib;

// The BMI this TU imports must have been compiled under this TU's own view
// of __cpp_exceptions/__cpp_rtti -- prog, prog_eh and prog_rtti each set
// their own view via cpp_eh/cpp_rtti, modlib is always the default.
#ifdef __cpp_exceptions
constexpr bool my_eh_view = true;
#else
constexpr bool my_eh_view = false;
#endif

#ifdef __cpp_rtti
constexpr bool my_rtti_view = true;
#else
constexpr bool my_rtti_view = false;
#endif

int main() {
    return (my_eh_view == built_with_eh && my_rtti_view == built_with_rtti) ? (f() - 42) : 1;
}
