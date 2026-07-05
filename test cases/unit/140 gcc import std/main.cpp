import std;

// `import std;`, resolved from the `dependency('std')` this target links.
int main() {
    std::string msg = "import std works";
    std::println("{}", msg);
    return msg.size() == 16 ? 0 : 1;
}
