import std;
import submod;

// Uses std directly and consumes a subproject module that also imports std.
int main() {
    std::string msg = "parent";
    return (sub_value() == 12 && msg.size() == 6) ? 0 : 1;
}
