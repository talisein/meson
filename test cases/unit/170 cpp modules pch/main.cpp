// <string> is included directly, not left to the PCH: the project must
// still build where the PCH is dropped (-Db_pch=false, or a compiler that
// cannot combine PCH with modules).
#include <string>

import modlib;

int main() {
    std::string s = "x";
    return (mval() == 42 && s.size() == 1) ? 0 : 1;
}
