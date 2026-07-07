// import std and a header unit in one translation unit: the std module's
// BMI (found through the synthesized library's provided-module map) and the
// declared unit's pre-built BMI must both resolve on the same commands.
import std;
import "hu.h";

int main() {
    return std::string("meson").size() + hu_val() == 10 ? 0 : 1;
}
