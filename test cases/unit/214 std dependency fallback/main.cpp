// Deliberately does not `import std`: the fallback provides a plain declared
// dependency (no std modules), so this program must link on either side --
// whether the compiler-synthesized std module library or the fallback was
// returned.
int main() {
    return 0;
}
