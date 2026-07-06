import "util.h";  // the same user header unit as prog -> one shared build edge

int main() {
    return util_val() == 7 ? 0 : 1;
}
