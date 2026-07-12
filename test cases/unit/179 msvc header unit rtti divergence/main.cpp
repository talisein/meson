import "util.h";

#ifdef _CPPRTTI
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_rtti ? 0 : 1;
}
