import tests;
import libmod;

int main() {
    // Only correct if both the private "tests" module and the shared
    // "libmod" dependency module resolved to the right BMI.
    return (answer() + libval() == 42) ? 0 : 1;
}
