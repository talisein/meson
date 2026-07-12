import "util.h";

#ifdef _CPPUNWIND
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_eh ? 0 : 1;
}
