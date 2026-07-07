import "util.h";       // a user header unit (quote spelling)
import <angleutil.h>;  // a system header unit (angle spelling)
import mod;            // an ordinary named module, in the same target

int main() {
    return (util_val() + mod_val() + angle_val()) == 110 ? 0 : 1;
}
