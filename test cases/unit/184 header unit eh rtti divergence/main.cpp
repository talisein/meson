import "util.h";

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
    return (my_eh_view == built_with_eh && my_rtti_view == built_with_rtti) ? 0 : 1;
}
