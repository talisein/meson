import std;
import cxxmod;

int main() {
    std::string s = "ok";
    return (s == "ok" && cxxvalue() == 42) ? 0 : 1;
}
