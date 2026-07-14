import foo;

// Consumes the module 'foo', whose BMI lives in this target's machine's own
// class subdir -- never the other machine's, though both name the module 'foo'.
int main() {
    return f() - 1;
}
