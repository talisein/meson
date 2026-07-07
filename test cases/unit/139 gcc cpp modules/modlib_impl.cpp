// A module implementation unit: `module modlib;` (no export keyword)
// implicitly imports the interface, so the scan must order this TU after
// modlib's BMI even though nothing here is spelled `import`.
module modlib;

int implfunc() {
    return 20;
}
