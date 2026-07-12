// The executable's own private module, sharing its name with the one sub1/util
// provides privately: the two would emit the same linkage symbols into one
// binary.
export module detail;

export int detail_value() {
    return 4;
}
