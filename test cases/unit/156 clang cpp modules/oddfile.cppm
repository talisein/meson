// The module name deliberately differs from the file name: Clang writes this
// BMI as oddfile.cppm.pcm next to the object, and the harvest edge must
// publish it as pcm.cache/oddname.pcm for the import to resolve.
export module oddname;

export int oddfunc() {
    return 7;
}
