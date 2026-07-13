import "configured.h";
import util;

int main() {
    return configured_value() + util_val() == 10 ? 0 : 1;
}
