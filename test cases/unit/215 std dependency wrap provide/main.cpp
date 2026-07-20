// Does not `import std`: the wrap fallback provides a plain declared
// dependency, so this must link whether synthesis or the wrap subproject
// supplied the dependency.
int main() {
    return 0;
}
